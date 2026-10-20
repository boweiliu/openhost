from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from enum import StrEnum

import cattrs

import openhost_system_agent.updater.progress as agent_progress
from compute_space.core.logging import logger
from compute_space.core.util import async_wrap
from openhost_system_agent.detach import APPLY_UNIT
from openhost_system_agent.protocol import DiffResult
from openhost_system_agent.protocol import FetchResult
from openhost_system_agent.protocol import MigrationStatus
from openhost_system_agent.protocol import RemoteInfo
from openhost_system_agent.updater.paths import DATA_DIR_ENV


class SystemAgentError(Exception):
    pass


# The agent resolves the shared updater paths (progress log, token, certs) from
# OPENHOST_DATA_DIR, but sudo's env_reset strips it. Forward it explicitly (via
# `sudo env`) so a non-default data_root_dir keeps compute_space and the agent
# pointed at the same files instead of silently diverging to the agent's default.
def _agent_argv(*args: str) -> list[str]:
    data_dir = os.environ.get(DATA_DIR_ENV)
    if data_dir:
        return ["sudo", "env", f"{DATA_DIR_ENV}={data_dir}", "openhost_system_agent", *args]
    return ["sudo", "openhost_system_agent", *args]


# Missing/unresolvable agent symlink. Usually needs an ansible re-deploy, but it
# also appears transiently right after a self-update restart before sudo's PATH
# lookup settles, so we retry before surfacing the "re-run ansible" guidance.
# (`sudo env` reports it as "No such file or directory" instead of sudo's form.)
_CMD_NOT_FOUND_PATTERNS = (
    "openhost_system_agent: command not found",
    "openhost_system_agent': No such file or directory",
)
_NOT_FOUND_RETRIES = 5
_NOT_FOUND_RETRY_DELAY = 0.5


def _is_cmd_not_found(error: str) -> bool:
    return any(p in error for p in _CMD_NOT_FOUND_PATTERNS)


def _run_system_agent(*args: str, timeout: int = 300) -> str:
    """Run the agent, raising SystemAgentError on failure. Returns stdout.

    Retries transient post-restart "command not found" errors (see
    _CMD_NOT_FOUND_PATTERNS); all other failures raise immediately.
    """
    last_not_found: str | None = None
    for attempt in range(_NOT_FOUND_RETRIES):
        try:
            result = subprocess.run(
                _agent_argv(*args),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise SystemAgentError("openhost_system_agent not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise SystemAgentError(f"openhost_system_agent timed out after {timeout}s") from e

        if result.returncode == 0:
            return result.stdout

        try:
            body = json.loads(result.stdout)
            error = body.get("error", result.stderr)
        except (json.JSONDecodeError, ValueError):
            error = result.stderr or result.stdout

        if _is_cmd_not_found(str(error)):
            last_not_found = str(error).strip()
            if attempt < _NOT_FOUND_RETRIES - 1:
                time.sleep(_NOT_FOUND_RETRY_DELAY)
                continue
            raise SystemAgentError(
                f"{last_not_found}\n"
                "\n"
                "The openhost_system_agent binary is not on sudo's PATH. "
                "Re-running the ansible deploy against this host will reinstall it — "
                "see https://github.com/cloud-in-a-bottle/cloud-in-a-bottle/blob/main/ansible/readme.md"
            )
        raise SystemAgentError(str(error))

    raise SystemAgentError(last_not_found or "openhost_system_agent failed")


def _call_system_agent_sync[ResultT](result_type: type[ResultT], *args: str, timeout: int = 300) -> ResultT:
    stdout = _run_system_agent(*args, timeout=timeout)
    try:
        raw = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as e:
        raise SystemAgentError(f"Invalid JSON from system agent: {stdout}") from e

    try:
        return cattrs.structure(raw, result_type)
    except (cattrs.ClassValidationError, KeyError, TypeError) as e:
        raise SystemAgentError(f"Unexpected response shape from system agent: {e}") from e


@async_wrap
def system_agent_fetch() -> FetchResult:
    return _call_system_agent_sync(FetchResult, "update", "fetch")


@async_wrap
def system_agent_show_diff() -> DiffResult:
    return _call_system_agent_sync(DiffResult, "update", "show-diff")


# Serializes update launches from the Settings button and the auto-update schedule.
apply_lock = asyncio.Lock()
_apply_tasks: set[asyncio.Task[None]] = set()
_APPLY_POLL_SECONDS = 2.0

# The agent's refusal when a walk is already running. Its log belongs to that walk,
# so don't terminate it here.
_ALREADY_RUNNING = "already in progress"


@async_wrap
def _start_apply() -> None:
    # The agent hands the walk to openhost-apply.service and returns immediately,
    # so this call is just the handoff — no output worth parsing. The walk then
    # stops openhost (killing this process), and the freshly started
    # compute_space reads the progress log for results.
    _run_system_agent("update", "apply", timeout=60)


class ApplyLaunch(StrEnum):
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    FAILED = "failed"


async def system_agent_apply() -> ApplyLaunch:
    """Hand the update to the detached apply unit. The caller must hold ``apply_lock``.

    An independent task owns the lock through launch and completion: cancelling the
    caller must not release it while the threaded handoff or detached walk continues.
    """
    launched: asyncio.Future[ApplyLaunch] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_launch_and_monitor_apply(launched), name="monitor-update-apply")
    _apply_tasks.add(task)
    task.add_done_callback(_apply_tasks.discard)
    return await asyncio.shield(launched)


@async_wrap
def _apply_finished() -> bool:
    # An inactive unit alone can be the gap before the detached walk starts.
    # start_apply resets the log before handing off, so only a terminal log AND
    # an inactive unit prove this walk finished. A failed preflight may be so
    # fast that we never observe the unit running.
    return not _apply_is_running() and agent_progress.is_terminal(agent_progress.read_entries())


def _apply_is_running() -> bool:
    # The agent's launch-time helper treats unavailable status as inactive (the
    # unit-name collision is its backstop). Unlocking needs confirmed inactivity.
    result = subprocess.run(
        ["systemctl", "show", "--property=ActiveState", "--value", APPLY_UNIT],
        capture_output=True,
        text=True,
        timeout=15,
    )
    state = result.stdout.strip()
    if result.returncode != 0 or state not in (
        "active",
        "activating",
        "deactivating",
        "reloading",
        "inactive",
        "failed",
    ):
        raise SystemAgentError(f"Could not determine detached apply state: {result.stderr or state}")
    return state not in ("inactive", "failed")


async def _launch_and_monitor_apply(launched: asyncio.Future[ApplyLaunch]) -> None:
    try:
        launch = await _launch_apply()
        launched.set_result(launch)
        if launch == ApplyLaunch.STARTED:
            while True:
                try:
                    if await _apply_finished():
                        break
                except Exception:
                    logger.exception("failed to check detached apply completion; retrying")
                await asyncio.sleep(_APPLY_POLL_SECONDS)
    except Exception as e:
        if not launched.done():
            launched.set_exception(e)
        else:
            logger.exception("detached apply monitor failed")
    finally:
        apply_lock.release()


async def _launch_apply() -> ApplyLaunch:
    try:
        await _start_apply()
    except Exception as e:
        if _ALREADY_RUNNING in str(e):
            # Started outside this process (e.g. from the CLI). Don't hold the lock for it: if it fails before
            # stopping openhost, nothing would ever release it.
            logger.warning("apply already in progress; leaving its progress log alone")
            return ApplyLaunch.ALREADY_RUNNING
        # Not just SystemAgentError: ANY failure must leave the log terminal, or
        # the /updating page would poll forever with no explanation.
        logger.exception("system agent apply failed")
        await record_apply_failure(f"Update failed: {e}")
        try:
            await system_agent_clear_update_token()
        except SystemAgentError:
            logger.exception("failed to clear update token")
        return ApplyLaunch.FAILED
    return ApplyLaunch.STARTED


async def record_apply_failure(message: str) -> None:
    """Ensure the log ends terminal so the /updating page stops polling. Falls back
    to the root agent for a root-owned log."""
    try:
        if agent_progress.record_failure_if_not_terminal(message):
            return
    except Exception:
        logger.exception("failed to record apply failure directly")
    try:
        await system_agent_record_update_failure(message)
    except SystemAgentError:
        logger.exception("failed to record apply failure via the agent")


@async_wrap
def system_agent_set_remote(url: str) -> RemoteInfo:
    return _call_system_agent_sync(RemoteInfo, "update", "set-remote", url, timeout=120)


@async_wrap
def system_agent_set_prereleases(enabled: bool) -> RemoteInfo:
    return _call_system_agent_sync(RemoteInfo, "update", "set-prereleases", "on" if enabled else "off")


@async_wrap
def system_agent_get_remote() -> RemoteInfo:
    return _call_system_agent_sync(RemoteInfo, "update", "get-remote")


@async_wrap
def system_agent_status() -> MigrationStatus:
    return _call_system_agent_sync(MigrationStatus, "status")


@async_wrap
def system_agent_set_update_token(token: str) -> None:
    _run_system_agent("updater", "set-token", token, timeout=30)


@async_wrap
def system_agent_clear_update_token() -> None:
    _run_system_agent("updater", "clear-token", timeout=30)


def system_agent_mark_boot_complete_sync() -> None:
    """Finalize the update progress log via the root agent (see mark_boot_complete)."""
    _run_system_agent("updater", "mark-booted", timeout=30)


@async_wrap
def system_agent_record_update_failure(message: str) -> None:
    """Record a terminal 'failed' progress entry via the root agent."""
    _run_system_agent("updater", "fail", message, timeout=30)


def system_agent_stop_updater_sync() -> None:
    """Stop the detached updater (releasing 80/443), synchronously. Best-effort.

    Failure is logged, not raised: Caddy still has its own bind-retry window, but
    if that also loses the race the instance has no TLS terminator — leave a trace
    of why the ports were never released.
    """
    try:
        _run_system_agent("updater", "stop", timeout=30)
    except SystemAgentError:
        logger.exception("failed to stop the detached updater; Caddy must win 80/443 via bind-retry")


def system_agent_reset_restart_limit_sync() -> None:
    """Reset systemd's start-rate counter before an intentional restart."""
    _run_system_agent("service", "reset-start-limit", timeout=30)
