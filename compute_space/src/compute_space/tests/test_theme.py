"""Tests for the theme option (Sky and Classic).

``theme.js`` stores the choice and sets ``data-theme`` on ``<html>``; ``theme-classic.css`` holds every
Classic rule behind ``:root[data-theme="classic"]``. Every page template has to load the script and the
stylesheet, or a page would fall back to Sky while the rest of the space is Classic.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment
from jinja2 import FileSystemLoader
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.config import set_active_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes import docs
from compute_space.web.routes.pages.apps import dashboard
from compute_space.web.routes.pages.settings import settings_page

from ._litestar_helpers import auth_cookie
from .conftest import _make_test_config

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
STATIC_DIR = WEB_DIR / "static"
CLASSIC_PREFIX = ':root[data-theme="classic"]'


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path, zone_domain="alice-zone.example.com")
    init_db(config.db_path)
    yield config


def _build_app(cfg: Any) -> Litestar:
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=WEB_DIR / "templates", engine=JinjaTemplateEngine
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, STATIC_DIR))

    return Litestar(
        route_handlers=[dashboard, settings_page],
        template_config=template_config,
        dependencies={"config": Provide(provide_config, sync_to_thread=False), "db": Provide(provide_db)},
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _get_pages(cfg: Any, *paths: str) -> list[str]:
    """The HTML of each path, fetched as the signed-in owner."""
    set_active_config(cfg)
    cookie = auth_cookie(cfg, username="owner")
    pages: list[str] = []
    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        for path in paths:
            resp = client.get(path)
            assert resp.status_code == 200
            pages.append(resp.text)
    return pages


def _assert_loads_theme_assets(html: str) -> None:
    assert "js/theme.js" in html
    assert "css/theme-classic.css" in html
    # The script comes first so the theme is set before the stylesheets paint.
    assert html.index("js/theme.js") < html.index("css/tokens.css")
    assert html.index("css/components.css") < html.index("css/theme-classic.css")


def test_layout_pages_load_theme_assets(cfg: Any) -> None:
    for html in _get_pages(cfg, "/dashboard", "/settings"):
        _assert_loads_theme_assets(html)


def test_settings_page_has_a_button_for_every_theme(cfg: Any) -> None:
    (html,) = _get_pages(cfg, "/settings")
    assert "Appearance" in html
    for choice in _theme_choices():
        assert f'id="theme-{choice}"' in html
        # The button macro HTML-escapes the quotes in the onclick attribute.
        assert re.search(rf"setTheme\((?:'|&#39;){choice}(?:'|&#39;)\)", html)


def test_narrow_layout_loads_theme_assets() -> None:
    env = Environment(loader=FileSystemLoader(WEB_DIR / "templates"), autoescape=True)
    env.globals["static_url"] = lambda name: f"/static/{name}"
    html = env.get_template("login.html").render(next="/")
    _assert_loads_theme_assets(html)


def test_docs_page_template_loads_theme_assets() -> None:
    template = docs._TEMPLATE
    assert "js/theme.js" in template
    assert "css/theme-classic.css" in template


def _theme_choices() -> list[str]:
    js = (STATIC_DIR / "js" / "theme.js").read_text()
    match = re.search(r"const CHOICES = \[([^\]]*)\]", js)
    assert match is not None
    return re.findall(r"'([a-z]+)'", match.group(1))


def _default_theme() -> str:
    js = (STATIC_DIR / "js" / "theme.js").read_text()
    match = re.search(r"const DEFAULT_CHOICE = '([a-z]+)'", js)
    assert match is not None
    return match.group(1)


def test_every_non_default_theme_has_a_stylesheet_block() -> None:
    css = "".join(path.read_text() for path in STATIC_DIR.glob("css/*.css"))
    choices = _theme_choices()
    assert _default_theme() in choices
    for choice in choices:
        if choice != _default_theme():
            assert f':root[data-theme="{choice}"]' in css


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_classic_rules_are_all_scoped_to_the_classic_theme() -> None:
    css = _strip_comments((STATIC_DIR / "css" / "theme-classic.css").read_text())
    selectors = [s.strip() for block in re.findall(r"([^{}]+)\{", css) for s in block.split(",")]
    assert selectors
    unscoped = [s for s in selectors if not s.startswith(CLASSIC_PREFIX)]
    assert unscoped == []


def test_classic_only_overrides_tokens_that_exist() -> None:
    tokens = _strip_comments((STATIC_DIR / "css" / "tokens.css").read_text())
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", tokens, flags=re.M))
    classic = _strip_comments((STATIC_DIR / "css" / "theme-classic.css").read_text())
    # Declarations only: a class like .btn--primary:hover must not count as a token.
    overridden = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", classic, flags=re.M))
    assert overridden
    assert overridden <= defined, f"unknown tokens: {sorted(overridden - defined)}"


def test_default_theme_is_the_one_the_page_ships_with() -> None:
    assert _default_theme() == "sky"
