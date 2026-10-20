"""Quick reference and wiki analytics show only the caller's catalog view.

Real fixture directory, real group store, real registry, real WikiCache
(SQLite). Only configuration and the golden-repo manager wiring are
stand-ins.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator
from unittest.mock import MagicMock

import pytest

from code_indexer.server.mcp.handlers import guides
from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.wiki.wiki_cache import WikiCache
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    LANGFUSE_GRANTED,
    LANGFUSE_HIDDEN,
    RESTRICTED_USER,
    CidxMetaFixture,
    add_depmap_extension,
    build_cidx_meta_fixture,
)
from tests.unit.server.mcp.depmap_visibility_support import (
    assert_hidden_absent,
    build_depmap_server,
    call_tool,
)

VIEWED = (
    ALPHA,
    CANARY,
    GAMMA,
    "README",
    "dependency-map/payments",
    f"dependency-map/{CANARY}",
    "dependency-map/inventory",
    "dependency-map/_journal",
    ".code-indexer/index/meta",
    f"xray-patterns/{CANARY}/p",
    "",
)


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path_factory.mktemp("guides-vis"))
    add_depmap_extension(fixture)
    return fixture


@pytest.fixture
def server(fx: CidxMetaFixture, tmp_path: Path) -> Iterator[SimpleNamespace]:
    wiki_db = tmp_path / "wiki.db"
    cache = WikiCache(str(wiki_db))
    cache.ensure_tables()
    for path in VIEWED:
        cache.increment_view("cidx-meta", path)
    golden = SimpleNamespace(
        golden_repos_dir=str(fx.root.parent),
        db_path=str(wiki_db),
        list_golden_repos=lambda: [
            {"alias": a} for a in (ALPHA, CANARY, LANGFUSE_GRANTED, LANGFUSE_HIDDEN)
        ],
        _sqlite_backend=SimpleNamespace(
            list_repos=lambda: [{"alias": "cidx-meta", "wiki_enabled": True}]
        ),
    )
    config = MagicMock()
    config.service_display_name = "Neo"
    config.langfuse_config.pull_enabled = True
    config.langfuse_config.pull_projects = ["sample"]
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            state = build_depmap_server(monkeypatch, fx, golden_repo_manager=golden)
            monkeypatch.setattr(
                guides,
                "get_config_service",
                lambda: SimpleNamespace(get_config=lambda: config),
            )
            yield state
    finally:
        DatabaseConnectionManager.get_instance(str(wiki_db)).close_all()


def _quick_reference(username: str) -> Dict[str, Any]:
    return call_tool("cidx_quick_reference", {}, username)


def test_quick_reference_counts_only_visible_domains(server: Any) -> None:
    restricted = _quick_reference(RESTRICTED_USER)
    admin = _quick_reference(ADMIN_USER)
    assert "(4 domains)" in restricted["dependency_map"]
    assert "(7 domains)" in admin["dependency_map"]


def test_quick_reference_lists_only_visible_trace_repositories(server: Any) -> None:
    restricted = _quick_reference(RESTRICTED_USER)
    assert_hidden_absent(restricted, extra=(LANGFUSE_HIDDEN,))
    section = restricted["langfuse_trace_search"]
    assert section["available_repositories"] == [LANGFUSE_GRANTED]
    admin = _quick_reference(ADMIN_USER)
    assert admin["langfuse_trace_search"]["available_repositories"] == [
        LANGFUSE_GRANTED,
        LANGFUSE_HIDDEN,
    ]


def _analytics(username: str) -> Dict[str, Any]:
    return call_tool(
        "wiki_article_analytics",
        {"repo_alias": "cidx-meta-global", "limit": 500},
        username,
    )


def test_wiki_analytics_lists_no_denied_catalog_article(server: Any) -> None:
    data = _analytics(RESTRICTED_USER)
    text = assert_hidden_absent(data)
    assert ".code-indexer" not in text and "_journal" not in text
    paths = sorted(a["path"] for a in data["articles"])
    assert paths == sorted([ALPHA, "dependency-map/payments"])
    assert data["total_count"] == len(data["articles"])


def test_wiki_analytics_admin_lists_every_article(server: Any) -> None:
    data = _analytics(ADMIN_USER)
    assert sorted(a["path"] for a in data["articles"]) == sorted(VIEWED)
    assert json.dumps(data).count(CANARY) > 0
