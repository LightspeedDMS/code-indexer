"""Re-check visibility when serving cached catalog content.

A handle minted while a request served the internal catalog carries the
caller's visibility at write time; every read door re-checks the caller's
CURRENT visibility, so a revoked (or added) grant makes the handle answer
like an expired one. Real SQLite payload cache, real group store, the real
read doors. Neutral names only.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.cache.handle_binding import reset_handle_binding
from code_indexer.server.cache.payload_cache import (
    CacheNotFoundError,
    PayloadCache,
    PayloadCacheConfig,
)
from code_indexer.server.mcp.handlers import _utils
from code_indexer.server.services.cidx_meta_access.cached_handles import (
    bind_catalog_handles,
    require_catalog_handle_visible,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

TEXT = "x" * 9000  # larger than one preview


class _Activations:
    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Any:
        if user_alias == "team-notes":
            return {"user_alias": user_alias, "golden_repo_alias": "cidx-meta"}
        if user_alias == "my-alpha":
            return {"user_alias": user_alias, "golden_repo_alias": ALPHA}
        return None


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


@pytest.fixture
def cache(tmp_path: Path) -> Iterator[PayloadCache]:
    payload_cache = PayloadCache(tmp_path / "payload.db", PayloadCacheConfig())
    payload_cache.initialize()
    yield payload_cache
    payload_cache.close()


def _team_id(fx: CidxMetaFixture) -> int:
    team = fx.group_manager.get_group_by_name("fixture-team")
    assert team is not None
    return int(team.id)


def _bound_handle(
    cache: PayloadCache, fx: CidxMetaFixture, username: str, alias: Any
) -> str:
    token = bind_catalog_handles(alias, username, fx.access_service, _Activations())
    try:
        return cache.store(TEXT, owner=username)
    finally:
        reset_handle_binding(token)


def test_bound_handle_requires_unchanged_visibility(
    cache: PayloadCache, fx: CidxMetaFixture
) -> None:
    handle = _bound_handle(cache, fx, RESTRICTED_USER, "cidx-meta-global")
    require_catalog_handle_visible(handle, RESTRICTED_USER, fx.access_service)
    assert cache.retrieve_owned(handle, RESTRICTED_USER).content == TEXT[:5000]

    fx.group_manager.revoke_repo_access(ALPHA, _team_id(fx))
    with pytest.raises(CacheNotFoundError):
        require_catalog_handle_visible(handle, RESTRICTED_USER, fx.access_service)


def test_added_grant_also_expires_a_bound_handle(
    cache: PayloadCache, fx: CidxMetaFixture
) -> None:
    handle = _bound_handle(cache, fx, RESTRICTED_USER, "team-notes")
    fx.group_manager.grant_repo_access(GAMMA, _team_id(fx), granted_by="test")
    with pytest.raises(CacheNotFoundError):
        require_catalog_handle_visible(handle, RESTRICTED_USER, fx.access_service)


def test_batch_handles_are_bound_too(cache: PayloadCache, fx: CidxMetaFixture) -> None:
    token = bind_catalog_handles(
        ["cidx-meta-global", f"{ALPHA}-global"],
        RESTRICTED_USER,
        fx.access_service,
        _Activations(),
    )
    try:
        handles = cache.store_batch([TEXT, TEXT], owner=RESTRICTED_USER)
    finally:
        reset_handle_binding(token)
    fx.group_manager.revoke_repo_access(ALPHA, _team_id(fx))
    for handle in handles:
        with pytest.raises(CacheNotFoundError):
            require_catalog_handle_visible(handle, RESTRICTED_USER, fx.access_service)


@pytest.mark.parametrize("alias", [f"{ALPHA}-global", "my-alpha", ["my-alpha"]])
def test_other_repositories_mint_unbound_handles(
    cache: PayloadCache, fx: CidxMetaFixture, alias: Any
) -> None:
    handle = _bound_handle(cache, fx, RESTRICTED_USER, alias)
    fx.group_manager.revoke_repo_access(ALPHA, _team_id(fx))
    require_catalog_handle_visible(handle, RESTRICTED_USER, fx.access_service)


@pytest.mark.parametrize(
    "alias", ["cidx-meta", "team-notes", ["x-global", "cidx-meta-global"], ["*"]]
)
def test_requests_that_may_serve_the_catalog_bind(
    cache: PayloadCache, fx: CidxMetaFixture, alias: Any
) -> None:
    handle = _bound_handle(cache, fx, RESTRICTED_USER, alias)
    fx.group_manager.revoke_repo_access(ALPHA, _team_id(fx))
    with pytest.raises(CacheNotFoundError):
        require_catalog_handle_visible(handle, RESTRICTED_USER, fx.access_service)


def test_admin_handle_stays_readable_while_admin(
    cache: PayloadCache, fx: CidxMetaFixture
) -> None:
    handle = _bound_handle(cache, fx, ADMIN_USER, "cidx-meta-global")
    fx.group_manager.revoke_repo_access(ALPHA, _team_id(fx))
    require_catalog_handle_visible(handle, ADMIN_USER, fx.access_service)


def test_missing_access_service_refuses_a_bound_handle(
    cache: PayloadCache, fx: CidxMetaFixture
) -> None:
    handle = _bound_handle(cache, fx, RESTRICTED_USER, "cidx-meta-global")
    with pytest.raises(CacheNotFoundError):
        require_catalog_handle_visible(handle, RESTRICTED_USER, None)


# ---------------------------------------------------------------------------
# The read doors
# ---------------------------------------------------------------------------


def _user(name: str) -> User:
    return User(
        username=name,
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def doors(
    cache: PayloadCache, fx: CidxMetaFixture, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    state = SimpleNamespace(
        payload_cache=cache, access_filtering_service=fx.access_service
    )
    monkeypatch.setattr(
        _utils, "app_module", SimpleNamespace(app=SimpleNamespace(state=state))
    )
    return SimpleNamespace(cache=cache, fx=fx)


def _mcp_get_cached(handle: str) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.search.cached_content import (
        handle_get_cached_content,
    )

    result = handle_get_cached_content({"handle": handle}, _user(RESTRICTED_USER))
    return dict(json.loads(result["content"][0]["text"]))


def _mcp_fetch_payload(handle: str) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.xray._cached_payload import (
        handle_cidx_fetch_cached_payload,
    )

    user = _user(RESTRICTED_USER)
    result = handle_cidx_fetch_cached_payload({"cache_handle": handle}, user)
    return dict(json.loads(result["content"][0]["text"]))


def _rest_get_cached(doors: SimpleNamespace, handle: str) -> Any:
    from code_indexer.server.routers.cache import router

    app = FastAPI()
    app.include_router(router)
    app.state.payload_cache = doors.cache
    app.state.access_filtering_service = doors.fx.access_service
    app.dependency_overrides[dependencies.get_current_user] = lambda: _user(
        RESTRICTED_USER
    )
    with TestClient(app) as client:
        return client.get(f"/cache/{handle}")


def test_mcp_get_cached_content_rechecks_visibility(doors: SimpleNamespace) -> None:
    handle = _bound_handle(doors.cache, doors.fx, RESTRICTED_USER, "cidx-meta-global")
    assert _mcp_get_cached(handle)["success"] is True
    doors.fx.group_manager.revoke_repo_access(ALPHA, _team_id(doors.fx))
    data = _mcp_get_cached(handle)
    assert data["success"] is False and data["error"] == "cache_expired", data


def test_mcp_fetch_cached_payload_rechecks_visibility(doors: SimpleNamespace) -> None:
    handle = _bound_handle(doors.cache, doors.fx, RESTRICTED_USER, "cidx-meta-global")
    assert _mcp_fetch_payload(handle)["success"] is True
    doors.fx.group_manager.revoke_repo_access(ALPHA, _team_id(doors.fx))
    data = _mcp_fetch_payload(handle)
    assert data["success"] is False and data["error"] == "cache_expired", data


def test_rest_cache_route_rechecks_visibility(doors: SimpleNamespace) -> None:
    handle = _bound_handle(doors.cache, doors.fx, RESTRICTED_USER, "cidx-meta-global")
    assert _rest_get_cached(doors, handle).status_code == 200
    doors.fx.group_manager.revoke_repo_access(ALPHA, _team_id(doors.fx))
    response = _rest_get_cached(doors, handle)
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "cache_expired"


def _search_code_handle(
    doors: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, alias: str
) -> str:
    """search_code with the per-repository search replaced by a double that
    caches one entry (as truncation does) and returns its handle."""
    from code_indexer.server.mcp.handlers.search import code_search

    def _search_writing_one_handle(params: Any, user: User, repo: str) -> Any:
        handle = doors.cache.store(TEXT, owner=user.username)
        return _utils._mcp_response({"success": True, "results": [], "h": handle})

    monkeypatch.setattr(code_search, "_search_global_repo", _search_writing_one_handle)
    result = code_search.search_code(
        {"repository_alias": alias, "query_text": "describe"}, _user(RESTRICTED_USER)
    )
    return str(json.loads(result["content"][0]["text"])["h"])


def test_search_code_on_the_catalog_binds_its_handles(
    doors: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _search_code_handle(doors, monkeypatch, "cidx-meta-global")
    require_catalog_handle_visible(handle, RESTRICTED_USER, doors.fx.access_service)
    doors.fx.group_manager.revoke_repo_access(ALPHA, _team_id(doors.fx))
    with pytest.raises(CacheNotFoundError):
        require_catalog_handle_visible(handle, RESTRICTED_USER, doors.fx.access_service)


def test_search_code_on_another_repository_mints_plain_handles(
    doors: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _search_code_handle(doors, monkeypatch, f"{ALPHA}-global")
    doors.fx.group_manager.revoke_repo_access(ALPHA, _team_id(doors.fx))
    require_catalog_handle_visible(handle, RESTRICTED_USER, doors.fx.access_service)
