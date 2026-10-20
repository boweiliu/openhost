from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.exceptions import HTTPException
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

import compute_space.core.system_agent.client as client_mod
import compute_space.web.routes.api.settings as settings_mod
from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.core.system_agent import progress as update_progress
from compute_space.core.system_agent import update_token as seamless_update
from compute_space.core.system_agent.client import _apply_is_running
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.settings import updating_page
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.updater import paths as agent_paths
from openhost_system_agent.updater import progress as agent_progress

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config


async def _drain() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _fresh_apply_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Successful fake launches remain running unless a test ends the walk.
    monkeypatch.setattr(client_mod, "_apply_is_running", lambda: True)
    monkeypatch.setattr(client_mod, "_APPLY_POLL_SECONDS", 0)
    if client_mod.apply_lock.locked():
        client_mod.apply_lock.release()


@pytest.fixture
def token_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    calls: dict[str, list[str]] = {"persist": [], "clear": []}

    async def fake_persist(token: str) -> None:
        calls["persist"].append(token)

    async def fake_clear() -> None:
        calls["clear"].append("x")

    monkeypatch.setattr(settings_mod, "persist_update_token", fake_persist)
    monkeypatch.setattr(client_mod, "system_agent_clear_update_token", fake_clear)
    return calls


def _status(ok: bool = True, reason: str = "", msg: str = "ok") -> MigrationStatus:
    return MigrationStatus(ok=ok, reason=reason, message=msg, current_host_version=1, expected_version=1)


# ─────────────── token minting / persistence ───────────────


def test_new_token_unique_and_urlsafe() -> None:
    tokens = {seamless_update.new_update_token() for _ in range(200)}
    assert len(tokens) == 200
    assert len(next(iter(tokens))) >= 32


@pytest.mark.asyncio
async def test_persist_token_calls_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake_set(token: str) -> None:
        seen.append(token)

    monkeypatch.setattr(seamless_update, "system_agent_set_update_token", fake_set)
    await seamless_update.persist_update_token("abc123")
    assert seen == ["abc123"]


# ─────────────── apply_update endpoint ───────────────
# The endpoint gate/lock/token-lifecycle tests live in test_settings_host_prep.py;
# here we cover only what that file doesn't: failure paths of the background
# apply task and their progress-log side effects.


@pytest.fixture
def progress_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(agent_paths.DATA_DIR_ENV, str(tmp_path))
    (tmp_path / "updater").mkdir(parents=True, exist_ok=True)
    return tmp_path


async def _apply_and_launch() -> object:
    """Call the handler, then run the launch the way Litestar does after sending.

    The launch is deliberately a response background task, not a create_task:
    the walk stops openhost within moments, so the browser must already have the
    token-carrying response by then.
    """
    response = await settings_mod.apply_update.fn()
    assert response.background is not None
    await response.background()
    return response


@pytest.mark.asyncio
async def test_apply_third_call_after_failure_allowed(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]], progress_env: Path
) -> None:
    # After a failed apply the lock frees, so a retry is accepted (not 409).
    async def failing() -> None:
        raise client_mod.SystemAgentError("boom")

    async def status() -> MigrationStatus:
        return _status()

    monkeypatch.setattr(client_mod, "_start_apply", failing)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    await _apply_and_launch()
    assert not client_mod.apply_lock.locked()
    assert token_calls["clear"] == ["x"]
    # Retry: a fresh apply is accepted.

    async def ok() -> None:
        return None

    monkeypatch.setattr(client_mod, "_start_apply", ok)
    resp2 = await settings_mod.apply_update.fn()
    assert resp2.content.token
    # Launched successfully: the lock stays held, because the walk is now about
    # to stop this process and a second click must not race it.
    await resp2.background()
    assert client_mod.apply_lock.locked()


@pytest.mark.asyncio
async def test_apply_generic_exception_also_recorded(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]], progress_env: Path
) -> None:
    # Not just SystemAgentError: any failure must clear the token and terminate
    # the progress log.
    async def failing() -> None:
        raise RuntimeError("totally unexpected")

    async def status() -> MigrationStatus:
        return _status()

    monkeypatch.setattr(client_mod, "_start_apply", failing)
    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    await _apply_and_launch()

    assert not client_mod.apply_lock.locked()
    assert token_calls["clear"] == ["x"]
    view = update_progress.read_progress()
    assert view.terminal is True
    assert "totally unexpected" in str(view.entries[-1]["message"])


@pytest.mark.asyncio
async def test_apply_status_error_500_releases_lock(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]]
) -> None:
    async def status() -> MigrationStatus:
        raise client_mod.SystemAgentError("agent unreachable")

    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    with pytest.raises(HTTPException) as e:
        await settings_mod.apply_update.fn()
    assert e.value.status_code == 500
    assert not client_mod.apply_lock.locked()


@pytest.mark.asyncio
async def test_detached_preflight_failure_allows_manual_retry(
    monkeypatch: pytest.MonkeyPatch, token_calls: dict[str, list[str]], progress_env: Path
) -> None:
    running = False
    checked = asyncio.Event()
    loop = asyncio.get_running_loop()

    def is_running() -> bool:
        loop.call_soon_threadsafe(checked.set)
        return running

    async def status() -> MigrationStatus:
        return _status()

    async def launch() -> None:
        agent_progress.reset_progress()

    monkeypatch.setattr(settings_mod, "system_agent_status", status)
    monkeypatch.setattr(client_mod, "_start_apply", launch)
    monkeypatch.setattr(client_mod, "_apply_is_running", is_running)
    await _apply_and_launch()
    await asyncio.wait_for(checked.wait(), 5)
    await _drain()
    # A transient inactive state before startup must not allow a second launch.
    assert client_mod.apply_lock.locked()
    with pytest.raises(HTTPException) as exc:
        await settings_mod.apply_update.fn()
    assert exc.value.status_code == 409

    # Fast preflight failure: the monitor never observed the unit as active.
    agent_progress.record(agent_progress.Phase.FAILED, "Working tree is dirty")
    await asyncio.wait_for(asyncio.gather(*client_mod._apply_tasks), 5)
    assert not client_mod.apply_lock.locked()
    assert update_progress.read_progress().entries[-1]["message"] == "Working tree is dirty"

    # The previous monitor must not release the next apply's lock.
    running = True
    await _apply_and_launch()
    agent_progress.record(agent_progress.Phase.FAILED, "Second apply failure")
    await _drain()
    assert client_mod.apply_lock.locked()
    running = False
    await asyncio.wait_for(asyncio.gather(*client_mod._apply_tasks), 5)
    assert not client_mod.apply_lock.locked()


@pytest.mark.asyncio
async def test_apply_monitor_keeps_lock_while_terminal_unit_still_running(
    monkeypatch: pytest.MonkeyPatch, progress_env: Path
) -> None:
    running = True
    checked = asyncio.Event()
    loop = asyncio.get_running_loop()

    def is_running() -> bool:
        loop.call_soon_threadsafe(checked.set)
        return running

    async def launch() -> None:
        agent_progress.reset_progress()
        agent_progress.record(agent_progress.Phase.FAILED, "preflight failed")

    monkeypatch.setattr(client_mod, "_start_apply", launch)
    monkeypatch.setattr(client_mod, "_apply_is_running", is_running)
    await client_mod.apply_lock.acquire()
    assert await client_mod.system_agent_apply() == client_mod.ApplyLaunch.STARTED
    await asyncio.wait_for(checked.wait(), 5)
    await _drain()
    assert client_mod.apply_lock.locked()
    running = False
    await asyncio.wait_for(asyncio.gather(*client_mod._apply_tasks), 5)
    assert not client_mod.apply_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("launch_fails", [False, True])
async def test_cancelled_apply_caller_does_not_release_threaded_launch(
    monkeypatch: pytest.MonkeyPatch,
    token_calls: dict[str, list[str]],
    progress_env: Path,
    launch_fails: bool,
) -> None:
    entered = asyncio.Event()
    finish_launch = threading.Event()
    loop = asyncio.get_running_loop()

    def run(*args: str, **kwargs: object) -> str:
        agent_progress.reset_progress()
        loop.call_soon_threadsafe(entered.set)
        assert finish_launch.wait(5)
        if launch_fails:
            raise client_mod.SystemAgentError("handoff failed")
        return ""

    monkeypatch.setattr(client_mod, "_run_system_agent", run)
    await client_mod.apply_lock.acquire()
    caller = asyncio.create_task(client_mod.system_agent_apply())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert client_mod.apply_lock.locked()
    finally:
        finish_launch.set()

    if not launch_fails:
        await _drain()
        assert client_mod.apply_lock.locked()
        agent_progress.record(agent_progress.Phase.FAILED, "detached preflight failed")
        monkeypatch.setattr(client_mod, "_apply_is_running", lambda: False)
    await asyncio.wait_for(asyncio.gather(*client_mod._apply_tasks), 5)
    assert not client_mod.apply_lock.locked()
    assert token_calls["clear"] == (["x"] if launch_fails else [])


@pytest.mark.asyncio
async def test_cancelled_caller_after_handoff_leaves_monitor_running(
    monkeypatch: pytest.MonkeyPatch, progress_env: Path
) -> None:
    launch_returning = asyncio.Event()

    async def launch() -> None:
        agent_progress.reset_progress()
        launch_returning.set()

    async def caller() -> None:
        assert await client_mod.system_agent_apply() == client_mod.ApplyLaunch.STARTED
        await asyncio.Event().wait()

    monkeypatch.setattr(client_mod, "_start_apply", launch)
    await client_mod.apply_lock.acquire()
    request = asyncio.create_task(caller())
    await asyncio.wait_for(launch_returning.wait(), 5)
    await _drain()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert client_mod.apply_lock.locked()
    agent_progress.record(agent_progress.Phase.FAILED, "preflight failed")
    monkeypatch.setattr(client_mod, "_apply_is_running", lambda: False)
    await asyncio.wait_for(asyncio.gather(*client_mod._apply_tasks), 5)
    assert not client_mod.apply_lock.locked()


@pytest.mark.parametrize(("returncode", "state"), [(1, "inactive"), (0, ""), (0, "unknown")])
def test_apply_state_requires_confirmed_systemd_response(
    monkeypatch: pytest.MonkeyPatch, returncode: int, state: str
) -> None:
    monkeypatch.setattr(
        client_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode, stdout=state, stderr="query error"),
    )
    with pytest.raises(client_mod.SystemAgentError):
        _apply_is_running()


@pytest.mark.asyncio
async def test_apply_monitor_retries_unavailable_status_without_unlocking(
    monkeypatch: pytest.MonkeyPatch, progress_env: Path
) -> None:
    checked = asyncio.Event()
    loop = asyncio.get_running_loop()

    def unavailable() -> bool:
        loop.call_soon_threadsafe(checked.set)
        raise subprocess.TimeoutExpired("systemctl", 15)

    async def launch() -> None:
        agent_progress.reset_progress()
        agent_progress.record(agent_progress.Phase.FAILED, "preflight failed")

    monkeypatch.setattr(client_mod, "_start_apply", launch)
    monkeypatch.setattr(client_mod, "_apply_is_running", unavailable)
    await client_mod.apply_lock.acquire()
    assert await client_mod.system_agent_apply() == client_mod.ApplyLaunch.STARTED
    await asyncio.wait_for(checked.wait(), 5)
    await _drain()
    assert client_mod.apply_lock.locked()
    monkeypatch.setattr(client_mod, "_apply_is_running", lambda: False)
    await asyncio.wait_for(asyncio.gather(*client_mod._apply_tasks), 5)
    assert not client_mod.apply_lock.locked()


# ─────────────── /updates endpoint (compute_space) ───────────────


@pytest.mark.asyncio
async def test_update_progress_empty(progress_env: Path) -> None:
    resp = await settings_mod.update_progress.fn()
    assert resp.entries == [] and resp.terminal is False


@pytest.mark.asyncio
async def test_update_progress_entries(progress_env: Path) -> None:
    with open(agent_paths.progress_log_path(), "w") as f:
        f.write(json.dumps({"phase": "fetch", "message": "F"}) + "\n")
        f.write(json.dumps({"phase": "done", "message": "D"}) + "\n")
    resp = await settings_mod.update_progress.fn()
    assert [e["phase"] for e in resp.entries] == ["fetch", "done"]
    assert resp.terminal is True


# ─────────────── update_progress helpers ───────────────


@pytest.mark.asyncio
async def test_record_apply_failure_terminates_nonterminal_log(progress_env: Path) -> None:
    agent_progress.record("fetch", "Fetching…")
    await client_mod.record_apply_failure("it broke")
    v = update_progress.read_progress()
    assert v.terminal is True and "it broke" in str(v.entries[-1]["message"])


@pytest.mark.asyncio
async def test_record_apply_failure_keeps_existing_terminal(progress_env: Path) -> None:
    agent_progress.record(agent_progress.Phase.FAILED, "agent-side detail")
    await client_mod.record_apply_failure("vaguer compute_space message")
    v = update_progress.read_progress()
    assert [e["phase"] for e in v.entries] == [agent_progress.Phase.FAILED]
    assert v.entries[-1]["message"] == "agent-side detail"


def test_mark_boot_complete_falls_back_to_agent(progress_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A log created by an older build is root-owned; the direct append fails and
    # the boot hook must route through the root agent instead of giving up.
    calls = {"agent": 0}
    monkeypatch.setattr(agent_progress, "mark_boot_complete", lambda: False)
    monkeypatch.setattr(
        update_progress, "system_agent_mark_boot_complete_sync", lambda: calls.__setitem__("agent", calls["agent"] + 1)
    )
    update_progress.mark_boot_complete()
    assert calls["agent"] == 1


@pytest.mark.asyncio
async def test_record_apply_failure_falls_back_to_agent(progress_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_agent_fail(message: str) -> None:
        calls.append(message)

    monkeypatch.setattr(agent_progress, "record_failure_if_not_terminal", lambda m: False)
    monkeypatch.setattr(client_mod, "system_agent_record_update_failure", fake_agent_fail)
    await client_mod.record_apply_failure("it broke")
    assert calls == ["it broke"]


# ─────────────── /updating page render ───────────────


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path, zone_domain="update-zone.example.com")
    init_db(config.db_path)
    yield config


def _build_app(cfg: Any, template_dir: Path | None = None) -> Litestar:
    """A minimal app with the real /updating route and Jinja globals installed.

    Uses the production template config so the test exercises the same frozen
    environment the router boots with.
    """
    web_dir = Path(__file__).resolve().parents[1] / "web"
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=template_dir or web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[updating_page],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        on_startup=[_install_globals],
        openapi_config=None,
    )


def test_updating_page_renders_for_owner(cfg: Any) -> None:
    # The page is the user-visible centrepiece of the update flow; make sure the
    # template (include + static_url calls) actually renders.
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/updating")
    assert resp.status_code == 200
    assert "Updating this instance" in resp.text
    assert "update-progress.js" in resp.text
    assert "update-progress.css" in resp.text
