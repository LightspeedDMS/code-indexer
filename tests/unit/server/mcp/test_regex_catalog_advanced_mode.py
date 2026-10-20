"""Advanced regex mode is unavailable on repositories a restricted caller
sees through a filtered view, whenever the catalog is in the searched set,
and only matches the caller can see fill a result page. Real MCP handler,
real REST route and real ripgrep over the catalog fixture."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers.search.regex_search import handle_regex_search
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY
from tests.unit.server.query.catalog_search_env import (
    ADMIN,
    CATALOG_ALIAS,
    RESTRICTED,
    CatalogEnv,
    catalog_app,
)

pytestmark = pytest.mark.timeout(60)
REFUSAL = "advanced regex mode is not available for this repository"
ORDINARY = f"{ALPHA}-global"
ADVANCED_PATTERN = rf"# {ALPHA}(?=\n\n{ALPHA} talks to {CANARY})"
ADVANCED_OTHER = rf"# {ALPHA}(?=\n\n{ALPHA} talks to example-other)"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    tmp = tmp_path_factory.mktemp("regex-advanced-mode")
    with catalog_app(tmp) as catalog:
        cache = PayloadCache(db_path=tmp / "payload.db", config=PayloadCacheConfig())
        cache.initialize()
        previous = getattr(catalog.app.state, "payload_cache", None)
        catalog.app.state.payload_cache = cache
        try:
            yield catalog
        finally:
            catalog.app.state.payload_cache = previous
            cache.close()


def _mcp(user: Any, **args: Any) -> Dict[str, Any]:
    args.setdefault("repository_alias", CATALOG_ALIAS)
    response = asyncio.run(handle_regex_search(args, user))
    return dict(json.loads(response["content"][0]["text"]))


def _rest(env: CatalogEnv, user: Any, **body: Any) -> Any:
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies

    body.setdefault("repository_alias", CATALOG_ALIAS)
    env.app.dependency_overrides[dependencies.get_current_user] = lambda: user
    try:
        return TestClient(env.app).post("/api/regex/search", json=body)
    finally:
        env.app.dependency_overrides.pop(dependencies.get_current_user, None)


def _lines(matches: List[Dict[str, Any]]) -> List[Any]:
    return [(m["file_path"], m["line_number"]) for m in matches]


def test_advanced_mode_works_for_admins(env: CatalogEnv) -> None:
    hit = _mcp(ADMIN, pattern=ADVANCED_PATTERN, multiline=True, pcre2=True)
    assert _lines(hit["matches"]) == [(f"{ALPHA}.md", 1)]
    miss = _mcp(ADMIN, pattern=ADVANCED_OTHER, multiline=True, pcre2=True)
    assert miss["success"] is True and miss["matches"] == []


@pytest.mark.parametrize("pattern", [ADVANCED_PATTERN, ADVANCED_OTHER])
def test_mcp_advanced_mode_on_the_catalog_is_refused(
    env: CatalogEnv, pattern: str
) -> None:
    payload = _mcp(RESTRICTED, pattern=pattern, multiline=True, pcre2=True)
    assert payload == {"success": False, "error": REFUSAL}


def test_mcp_omni_advanced_mode_with_the_catalog_in_the_set_is_refused(
    env: CatalogEnv,
) -> None:
    payload = _mcp(
        RESTRICTED,
        pattern=ADVANCED_PATTERN,
        repository_alias=[ORDINARY, CATALOG_ALIAS],
        multiline=True,
        pcre2=True,
    )
    assert payload == {"success": False, "error": REFUSAL}
    allowed = _mcp(
        RESTRICTED, pattern="payments", repository_alias=[ORDINARY], pcre2=True
    )
    assert allowed["success"] is True and allowed["matches"]


def test_plain_regex_on_the_catalog_still_works(env: CatalogEnv) -> None:
    payload = _mcp(RESTRICTED, pattern="applies", multiline=True)
    assert _lines(payload["matches"]) == [(f"{ALPHA}.md", 6)]


@pytest.mark.parametrize(
    "alias", [CATALOG_ALIAS, [ORDINARY, CATALOG_ALIAS]], ids=["single", "omni"]
)
def test_rest_advanced_mode_on_the_catalog_is_refused(
    env: CatalogEnv, alias: Any
) -> None:
    body = {"pattern": ADVANCED_PATTERN, "multiline": True, "pcre2": True}
    response = _rest(env, RESTRICTED, repository_alias=alias, **body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "error_code": "advanced_regex_unavailable",
        "detail": REFUSAL,
    }
    admin = _rest(env, ADMIN, repository_alias=CATALOG_ALIAS, **body)
    assert admin.status_code == 200 and admin.json()["matches"]


@pytest.mark.parametrize("term", [CANARY, "example-other"])
def test_only_visible_matches_fill_the_page(env: CatalogEnv, term: str) -> None:
    args = {"pattern": f"{term}|applies", "include_patterns": [f"{ALPHA}.md"]}
    mcp = _mcp(RESTRICTED, max_results=1, **args)
    rest = _rest(env, RESTRICTED, max_results=1, **args).json()
    for page in (mcp, rest):
        assert _lines(page["matches"]) == [(f"{ALPHA}.md", 6)]
        assert page["total_matches"] == 1 and page["truncated"] is False
