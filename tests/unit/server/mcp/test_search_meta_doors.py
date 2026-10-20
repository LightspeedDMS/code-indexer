"""MCP search_code serves a restricted caller only their projected view of
the catalog: single global repository (semantic, FTS, hybrid), omni and
wildcard fan-out, payload-cache handles, and the search-event outcome.

Real app, real handler, real indexes and a real small-preview payload cache
(catalog_search_env); only the embedding provider is replaced.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers.search.code_search import search_code
from code_indexer.server.services.search_event_outcomes import (
    search_payload_outcome,
)
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY, GAMMA
from tests.unit.server.query.catalog_search_env import (
    ADMIN,
    CATALOG_ALIAS,
    RESTRICTED,
    CatalogEnv,
    catalog_app,
    dump,
)
from tests.unit.server.query.extension_filter_env_2047 import ranked_store_search

pytestmark = pytest.mark.timeout(60)
_HANDLE_KEY = re.compile(r"cache_handle$")


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    tmp = tmp_path_factory.mktemp("mcp-search-doors")
    with catalog_app(tmp) as catalog:
        cache = PayloadCache(
            db_path=tmp / "payload.db",
            config=PayloadCacheConfig(preview_size_chars=12),
        )
        cache.initialize()
        previous = getattr(catalog.app.state, "payload_cache", None)
        catalog.app.state.payload_cache = cache
        try:
            yield catalog
        finally:
            from code_indexer.server.multi.multi_search_service import (
                MultiSearchService,
            )

            MultiSearchService._reset_singleton()
            catalog.app.state.payload_cache = previous
            cache.close()


def _payload(response: Dict[str, Any]) -> Dict[str, Any]:
    return dict(json.loads(response["content"][0]["text"]))


def _search(user: Any, **params: Any) -> Dict[str, Any]:
    with ranked_store_search():
        return _payload(search_code(params, user))


def _results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = payload["results"]
    if isinstance(results, dict):
        results = results.get("results", [])
    return list(results)


def _handles(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if _HANDLE_KEY.search(key) and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_handles(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_handles(item))
    return found


@pytest.mark.parametrize("mode", ["semantic", "fts", "hybrid"])
def test_single_catalog_search_is_projected(env: CatalogEnv, mode: str) -> None:
    payload = _search(
        RESTRICTED,
        query_text="payments",
        repository_alias=CATALOG_ALIAS,
        search_mode=mode,
        limit=40,
    )
    assert payload["success"] is True
    dumped = dump(payload)
    assert CANARY not in dumped and GAMMA not in dumped
    assert ALPHA in dumped or "payments" in dump(_results(payload))
    assert _results(payload)
    if mode != "fts":  # FTS hits for this term never touch canary lines
        admin = _search(
            ADMIN,
            query_text="payments",
            repository_alias=CATALOG_ALIAS,
            search_mode=mode,
            limit=40,
        )
        assert CANARY in dump(admin)


def test_fts_for_the_canary_is_an_empty_success(env: CatalogEnv) -> None:
    payload = _search(
        RESTRICTED,
        query_text=CANARY,
        repository_alias=CATALOG_ALIAS,
        search_mode="fts",
        limit=10,
    )
    assert _results(payload) == []
    assert search_payload_outcome(payload) is None
    admin = _search(
        ADMIN,
        query_text=CANARY,
        repository_alias=CATALOG_ALIAS,
        search_mode="fts",
        limit=10,
    )
    assert _results(admin)


def test_omni_catalog_search_is_projected(env: CatalogEnv) -> None:
    payload = _search(
        RESTRICTED, query_text="payments", repository_alias=[CATALOG_ALIAS], limit=40
    )
    rows = _results(payload)
    dumped = dump(rows)
    assert CANARY not in dumped and GAMMA not in dumped
    assert rows


def test_omni_never_returns_ungranted_repository_hits(env: CatalogEnv) -> None:
    aliases = [CATALOG_ALIAS, f"{ALPHA}-global", f"{CANARY}-global"]
    payload = _search(
        RESTRICTED,
        query_text="ledger",
        repository_alias=aliases,
        search_mode="fts",
        limit=40,
        response_format="flat",
    )
    rows = _results(payload)
    sources = {row.get("source_repo") for row in rows}
    assert f"{ALPHA}-global" in sources
    assert f"{CANARY}-global" not in sources
    assert CANARY not in dump(rows)
    cache = env.app.state.payload_cache
    for handle in _handles(payload):
        assert CANARY not in cache.retrieve_full(handle).lower()
    admin = _search(
        ADMIN,
        query_text="ledger",
        repository_alias=aliases,
        search_mode="fts",
        limit=40,
        response_format="flat",
    )
    assert f"{CANARY}-global" in {row.get("source_repo") for row in _results(admin)}


def test_cached_handles_hold_only_projected_text(env: CatalogEnv) -> None:
    payload = _search(
        RESTRICTED,
        query_text="payments",
        repository_alias=CATALOG_ALIAS,
        search_mode="fts",
        limit=40,
    )
    handles = _handles(payload)
    assert handles, "the small preview size must mint handles"
    cache = env.app.state.payload_cache
    for handle in handles:
        assert CANARY not in cache.retrieve_full(handle).lower()


def test_handles_are_refused_after_a_revocation(env: CatalogEnv) -> None:
    from code_indexer.server.mcp.handlers.search.cached_content import (
        handle_get_cached_content,
    )

    payload = _search(
        RESTRICTED,
        query_text="payments",
        repository_alias=CATALOG_ALIAS,
        search_mode="fts",
        limit=40,
    )
    handle = _handles(payload)[0]
    gam = env.fx.group_manager
    team = gam.get_group_by_name("fixture-team")
    assert team is not None
    before = _payload(handle_get_cached_content({"handle": handle}, RESTRICTED))
    assert before.get("success") is True
    gam.revoke_repo_access(ALPHA, team.id)
    try:
        after = _payload(handle_get_cached_content({"handle": handle}, RESTRICTED))
        assert after.get("success") is not True
    finally:
        gam.grant_repo_access(ALPHA, team.id, granted_by="test")
