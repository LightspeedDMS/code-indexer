"""MCP regex_search serves a restricted caller only their projected view of
the catalog: path policy, a match kept only on a line shown verbatim (the
caller's pattern is never re-evaluated), its context projected, counts and
cached handles after projection. Real handler and real ripgrep over the
catalog fixture (catalog_search_env)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers.search.regex_search import handle_regex_search
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY, GAMMA
from tests.unit.server.query.catalog_search_env import (
    ADMIN,
    CATALOG_ALIAS,
    RESTRICTED,
    CatalogEnv,
    catalog_app,
    dump,
)

pytestmark = pytest.mark.timeout(60)
CONTEXT_LINES = 3
ALPHA_APPLIES_LINE = 6  # "alphabet ordering applies.", shown verbatim
VISIBLE = "applies"
CHANGED = "talks"  # only on a line naming hidden repositories
MULTILINE = r"talks to.*\n.*nightly"  # spans lines projection changes
SMALL_PREVIEW = 12  # forces payload truncation, so handles are minted


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    tmp = tmp_path_factory.mktemp("mcp-regex-doors")
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


def _regex(user: Any, **args: Any) -> Dict[str, Any]:
    args.setdefault("repository_alias", CATALOG_ALIAS)
    args.setdefault("context_lines", CONTEXT_LINES)
    response = asyncio.run(handle_regex_search(args, user))
    return dict(json.loads(response["content"][0]["text"]))


def _matches(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    assert payload.get("success") is True, payload
    return list(payload["matches"])


def _served(payload: Dict[str, Any]) -> str:
    """Everything served except the echo of the caller's own pattern."""
    return dump({k: v for k, v in payload.items() if k != "pattern"})


def test_regex_for_the_canary_returns_nothing(env: CatalogEnv) -> None:
    payload = _regex(RESTRICTED, pattern=CANARY)
    assert _matches(payload) == []
    assert payload["total_matches"] == 0
    assert _matches(_regex(ADMIN, pattern=CANARY))


def test_regex_matches_are_projected_with_their_context(env: CatalogEnv) -> None:
    payload = _regex(RESTRICTED, pattern=VISIBLE)
    matches = _matches(payload)
    assert [m["file_path"] for m in matches] == [f"{ALPHA}.md"]
    assert matches[0]["line_number"] == ALPHA_APPLIES_LINE
    assert matches[0]["line_content"] == "alphabet ordering applies."
    assert len(matches[0]["context_before"]) == CONTEXT_LINES
    assert "[restricted repository]" in matches[0]["context_before"][0]
    served = _served(payload)
    assert CANARY not in served and GAMMA not in served


def test_regex_match_on_a_changed_line_is_dropped(env: CatalogEnv) -> None:
    assert _matches(_regex(ADMIN, pattern=CHANGED))
    payload = _regex(RESTRICTED, pattern=CHANGED)
    assert _matches(payload) == [] and payload["total_matches"] == 0


def test_dependency_map_matches_are_projected(env: CatalogEnv) -> None:
    payload = _regex(RESTRICTED, pattern="payments")
    paths = {m["file_path"] for m in _matches(payload)}
    assert "dependency-map/payments.md" in paths
    served = _served(payload)
    assert CANARY not in served and GAMMA not in served


def test_multiline_match_over_changed_lines_is_dropped(env: CatalogEnv) -> None:
    admin = _matches(_regex(ADMIN, pattern=MULTILINE, multiline=True))
    assert any(CANARY in m["line_content"] for m in admin)
    payload = _regex(RESTRICTED, pattern=MULTILINE, multiline=True)
    assert _matches(payload) == []
    served = _served(payload)
    assert CANARY not in served and GAMMA not in served


def test_regex_handles_hold_only_projected_text(env: CatalogEnv) -> None:
    cache = env.app.state.payload_cache
    default_preview = cache.config.preview_size_chars
    cache.config.preview_size_chars = SMALL_PREVIEW
    try:
        payload = _regex(RESTRICTED, pattern="payments")
    finally:
        cache.config.preview_size_chars = default_preview
    handles = [
        v
        for m in _matches(payload)
        for k, v in m.items()
        if k.endswith("cache_handle") and isinstance(v, str)
    ]
    assert handles, payload
    from code_indexer.server.cache.handle_binding import handle_binding
    from code_indexer.server.cache.payload_cache import CacheNotFoundError
    from code_indexer.server.services.cidx_meta_access import (
        require_catalog_handle_visible,
        resolve_meta_visibility,
    )

    service = env.fx.access_service
    view = resolve_meta_visibility(RESTRICTED.username, service).vis_hash
    for handle in handles:
        full = cache.retrieve_full(handle).lower()
        assert CANARY not in full and GAMMA not in full
        assert handle_binding(handle) == view
        with pytest.raises(CacheNotFoundError):
            require_catalog_handle_visible(handle, ADMIN.username, service)


def _restrict(env: CatalogEnv, user: str, alias: Any, arm: Any, registry: Any) -> Any:
    from code_indexer.server.services.cidx_meta_access.catalog_regex import (
        restrict_regex_matches,
    )

    repo = env.root.parent / "alpha-repo"
    raw = [
        {
            "file_path": "README.md",
            "line_number": 1,
            "column": 1,
            "line_content": (repo / "README.md").read_text().splitlines()[0],
            "context_before": [],
            "context_after": [],
        }
    ]
    return restrict_regex_matches(
        raw,
        alias=alias,
        repo_path=repo,
        username=user,
        access_service=env.fx.access_service,
        activated_repo_manager=arm,
        registry=registry,
    )


@pytest.mark.parametrize(
    "alias, unknown_dir",
    [(f"{ALPHA}-global", True), ("", False), ([], False)],
    ids=["unknown-golden-dir", "empty-alias", "empty-list-alias"],
)
def test_restricted_regex_fails_closed(
    env: CatalogEnv, alias: Any, unknown_dir: bool
) -> None:
    from tests.shared.cidx_meta_visibility_fixture import ADMIN_USER, RESTRICTED_USER

    arm = None if unknown_dir else env.app.state.activated_repo_manager
    registry = env.fx.registry
    # An ordinary file judged as a catalog path is denied: never served raw.
    assert _restrict(env, RESTRICTED_USER, alias, arm, registry) == []
    assert _restrict(env, ADMIN_USER, alias, arm, registry) is None


def test_restricted_regex_without_registry_raises(env: CatalogEnv) -> None:
    from tests.shared.cidx_meta_visibility_fixture import RESTRICTED_USER

    with pytest.raises(RuntimeError):
        _restrict(env, RESTRICTED_USER, CATALOG_ALIAS, None, None)


def _rest_regex(env: CatalogEnv, **body: Any) -> Dict[str, Any]:
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies

    env.app.dependency_overrides[dependencies.get_current_user] = lambda: RESTRICTED
    try:
        response = TestClient(env.app).post("/api/regex/search", json=body)
    finally:
        env.app.dependency_overrides.pop(dependencies.get_current_user, None)
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_rest_regex_for_the_canary_returns_nothing(env: CatalogEnv) -> None:
    body = _rest_regex(env, repository_alias=CATALOG_ALIAS, pattern=CANARY)
    assert body["matches"] == [] and body["total_matches"] == 0


def test_rest_regex_matches_are_projected(env: CatalogEnv) -> None:
    body = _rest_regex(
        env, repository_alias=CATALOG_ALIAS, pattern=VISIBLE, context_lines=3
    )
    assert [m["file_path"] for m in body["matches"]] == [f"{ALPHA}.md"]
    assert "[restricted repository]" in body["matches"][0]["context_before"][0]
    served = _served(body)
    assert CANARY not in served and GAMMA not in served
    changed = _rest_regex(env, repository_alias=CATALOG_ALIAS, pattern=CHANGED)
    assert changed["matches"] == [] and changed["total_matches"] == 0


def test_rest_omni_regex_catalog_part_is_projected(env: CatalogEnv) -> None:
    pattern = f"{CANARY}|{VISIBLE}"
    body = _rest_regex(
        env, repository_alias=[CATALOG_ALIAS, f"{ALPHA}-global"], pattern=pattern
    )
    assert any(m.get("source_repo") == CATALOG_ALIAS for m in body["matches"])
    served = _served(body).replace(pattern, "")
    assert CANARY not in served and GAMMA not in served


def test_omni_regex_catalog_part_is_projected(env: CatalogEnv) -> None:
    pattern = f"{CANARY}|{VISIBLE}"
    payload = _regex(
        RESTRICTED, pattern=pattern, repository_alias=[CATALOG_ALIAS, f"{ALPHA}-global"]
    )
    matches = _matches(payload)
    assert any(
        m.get("source_repo") == CATALOG_ALIAS and m["file_path"] == f"{ALPHA}.md"
        for m in matches
    )
    served = _served(payload).replace(pattern, "")
    assert CANARY not in served and GAMMA not in served


def test_truncation_flags_never_reveal_hidden_matches(env: CatalogEnv) -> None:
    assert _regex(ADMIN, pattern=CANARY, max_results=1)["truncated"] is True
    payload = _regex(RESTRICTED, pattern=CANARY, max_results=1)
    assert _matches(payload) == []
    assert payload["truncated"] is False and payload["read_capped"] is False
    body = _rest_regex(
        env, repository_alias=CATALOG_ALIAS, pattern=CANARY, max_results=1
    )
    assert body["matches"] == []
    assert body["truncated"] is False and body["read_capped"] is False
