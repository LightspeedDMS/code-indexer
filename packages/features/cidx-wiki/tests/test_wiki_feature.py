"""The cidx-wiki Feature object, as the SDK contract sees it."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, cast

import pytest
from fastapi.routing import APIRoute

from cidx_sdk import ROUTER_AUTH_HYBRID, Feature, sdk_satisfies
from cidx_sdk.capabilities import ACCESS_PROJECTION, CONFIG, REPOSITORY_EVENTS
from cidx_sdk.feature import HEALTHY
from cidx_wiki import __version__
from cidx_wiki.config import WIKI_CONFIG_SECTION, WikiConfig, wiki_config_from
from cidx_wiki.feature import REQUIRED_CAPABILITIES, WikiFeature, make_feature
from cidx_wiki.server import catalog_view, runtime
from cidx_wiki.server.migrations import WIKI_MIGRATIONS
from cidx_wiki.server.wiki_cache_invalidator import WikiCacheInvalidator

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture
def no_context_left() -> Iterator[None]:
    """Leave no feature context behind."""
    yield
    runtime.CONTEXT.uninstall()


class _Context:
    """A started feature's context holding the given capabilities."""

    def __init__(self, capabilities: Dict[str, object]) -> None:
        self._capabilities = capabilities

    def has_capability(self, name: str) -> bool:
        return name in self._capabilities

    def capability(self, name: str) -> object:
        return self._capabilities[name]


def test_factory_returns_a_feature_named_wiki() -> None:
    feature = make_feature()
    assert isinstance(feature, Feature)
    assert isinstance(feature, WikiFeature)
    assert feature.manifest.name == "wiki"
    assert feature.manifest.version == __version__
    assert sdk_satisfies(feature.manifest.requires_sdk)


def test_requires_the_access_projection_and_repository_events() -> None:
    required = make_feature().manifest.required_capabilities
    assert required == REQUIRED_CAPABILITIES
    assert ACCESS_PROJECTION in required and REPOSITORY_EVENTS in required


def test_entry_point_is_declared_under_the_feature_name() -> None:
    text = _PYPROJECT.read_text(encoding="utf-8")
    group = re.search(
        r'^\[project\.entry-points\."code_indexer\.features"\]\n(.*?)(?:\n\[|\Z)',
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert group is not None, "no code_indexer.features entry-point group"
    assert 'wiki = "cidx_wiki.feature:make_feature"' in group.group(1)


def test_tools_ship_their_docs_in_the_wheel() -> None:
    tools = make_feature().tools()
    assert [tool.name for tool in tools] == ["wiki_article_analytics"]
    assert tools[0].doc_markdown.startswith("---\nname: wiki_article_analytics\n")


def _paths(router: Any) -> List[Tuple[str, str]]:
    return [
        (route.path, route.name)
        for route in router.routes
        if isinstance(route, APIRoute)
    ]


def test_routers_put_the_search_routes_before_the_catch_all() -> None:
    search, pages, admin = make_feature().routers()
    assert search.prefix == pages.prefix == "/wiki"
    assert {name for _, name in _paths(search.router)} == {
        "wiki_search",
        "user_wiki_search",
    }
    assert search.auth == pages.auth == admin.auth == ROUTER_AUTH_HYBRID
    assert search.login_redirect is False and pages.login_redirect is True
    assert pages.static_dir is not None and Path(pages.static_dir, "wiki.css").is_file()
    assert "/{repo_alias}/{path:path}" in {path for path, _ in _paths(pages.router)}
    assert admin.prefix == "/admin" and admin.required_permission == "manage_users"


def test_config_section_is_the_former_wiki_config() -> None:
    section = make_feature().config_section()
    assert section is WIKI_CONFIG_SECTION
    assert {key.name: key.default for key in section.keys} == WikiConfig().__dict__
    assert not section.bootstrap_keys


def test_config_section_is_titled_and_labelled_for_the_config_screen() -> None:
    assert WIKI_CONFIG_SECTION.title == "Wiki Configuration"
    assert all(key.label for key in WIKI_CONFIG_SECTION.keys)


def test_wiki_config_from_ignores_unknown_keys() -> None:
    config = wiki_config_from({"enable_article_number": False, "other": 1})
    assert config == WikiConfig(enable_article_number=False)


def test_wiki_config_reads_the_config_capability(no_context_left: None) -> None:
    class _Section:
        def section(self) -> Dict[str, object]:
            return {"metadata_display_order": "author,created"}

    assert runtime.wiki_config() == WikiConfig()
    runtime.CONTEXT.install(cast(Any, _Context({CONFIG: _Section()})))
    assert runtime.wiki_config().metadata_display_order == "author,created"


def test_catalog_view_asks_the_access_projection(no_context_left: None) -> None:
    calls: List[Tuple[str, str, str, Optional[str]]] = []

    class _Projection:
        def catalog_view(
            self, viewer: str, repo_dir: str, *, alias: str, owner: Optional[str] = None
        ) -> None:
            calls.append((viewer, repo_dir, alias, owner))
            return None

    assert catalog_view.restricted_catalog_view("u", Path("/r"), alias="a") is None
    runtime.CONTEXT.install(cast(Any, _Context({ACCESS_PROJECTION: _Projection()})))
    catalog_view.restricted_catalog_view("u", Path("/r"), alias="a", owner="o")
    assert calls == [("u", "/r", "a", "o")]


def test_migration_1_creates_the_sqlite_tables() -> None:
    (migration,) = WIKI_MIGRATIONS
    assert migration.version == 1
    connection = sqlite3.connect(":memory:")
    migration.apply(connection)
    migration.apply(connection)  # idempotent
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"wiki_cache", "wiki_sidebar_cache", "wiki_article_views"} <= tables


class _Cursor:
    def __init__(self, rows: List[Any], executed: List[str]) -> None:
        self._rows = rows
        self._executed = executed

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._executed.append(sql)

    def fetchone(self) -> Any:
        return self._rows.pop(0) if self._rows else None


class _PgConnection:
    def __init__(self, rows: List[Any]) -> None:
        self.rows = rows
        self.executed: List[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.rows, self.executed)


def test_migration_1_imports_core_024_without_ddl() -> None:
    (migration,) = WIKI_MIGRATIONS
    pg = _PgConnection([(True,), (1,)])
    migration.apply(pg)
    assert not any("CREATE" in sql for sql in pg.executed)


def test_migration_1_creates_article_views_when_024_never_ran() -> None:
    (migration,) = WIKI_MIGRATIONS
    pg = _PgConnection([(True,), None])
    migration.apply(pg)
    assert any(
        "CREATE TABLE IF NOT EXISTS wiki_article_views" in s for s in pg.executed
    )


class _RecordingCache:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def invalidate_repo(self, alias: str) -> None:
        self.calls.append(("invalidate", alias))

    def delete_views_for_repo(self, alias: str) -> None:
        self.calls.append(("delete_views", alias))


def test_invalidator_answers_repository_events() -> None:
    cache = _RecordingCache()
    invalidator = WikiCacheInvalidator()
    invalidator.set_wiki_cache_factory(lambda: cast(Any, cache))
    invalidator.content_changed("example", "docs/page.md")
    invalidator.content_changed("example", "src/main.py")
    invalidator.content_changed("example", None)
    invalidator.repository_removed("example")
    invalidator.repository_renamed("example", "renamed")
    assert cache.calls == [
        ("invalidate", "example"),
        ("invalidate", "example"),
        ("delete_views", "example"),
        ("invalidate", "example"),
    ]


def test_health_reports_the_packaged_assets() -> None:
    (report,) = make_feature().health()
    assert report.status == HEALTHY
    assert report.detail == {"missing_files": 0}
