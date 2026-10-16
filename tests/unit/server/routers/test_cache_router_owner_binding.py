"""REST ``GET /cache/{handle}``: cached content is served only to an
authenticated caller who created it.

- No credentials: 401, no content.
- An entry with no recorded creator, and any key-prefixed entry owned by
  another route (``discovery:...``, ``temporal_query:...``), answer the
  same 404 ``cache_expired`` body as a missing handle -- never a distinct
  answer revealing that the handle exists. Admins get no bypass.

Real PayloadCache over every storage path (see ``_owner_bound_stores``);
the real authentication dependency for the unauthenticated case.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.user_manager import User, UserManager, UserRole
from code_indexer.server.routers.cache import router as cache_router
from tests.unit.server.cache._owner_bound_stores import (  # noqa: F401 (fixture)
    ALICE,
    BOB,
    LARGE_CONTENT,
    CacheStore,
    cache_store_fixture,
)


def _user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="unused",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


def _client(cache_store: CacheStore, caller: Any = None) -> TestClient:
    app = FastAPI()
    app.include_router(cache_router)
    app.state.payload_cache = cache_store.cache
    if caller is not None:
        app.dependency_overrides[dependencies.get_current_user] = lambda: caller
    return TestClient(app)


def _expired_body(handle: str) -> Dict[str, str]:
    return {
        "error": "cache_expired",
        "message": "Cache handle has expired or does not exist",
        "handle": handle,
    }


class TestCacheRouterRequiresAuthentication:
    def test_request_without_credentials_is_refused_without_content(
        self, cache_store: CacheStore, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(
            dependencies, "jwt_manager", JWTManager(secret_key="unit-test-secret")
        )
        monkeypatch.setattr(
            dependencies,
            "user_manager",
            UserManager(users_file_path=str(tmp_path / "users.json")),
        )
        handle = cache_store.cache.store(LARGE_CONTENT)

        response = _client(cache_store).get(f"/cache/{handle}")

        assert response.status_code == 401
        assert LARGE_CONTENT[:50] not in response.text


class TestCacheRouterServesOnlyOwnedEntries:
    def test_entry_without_recorded_creator_is_not_served(
        self, cache_store: CacheStore
    ) -> None:
        handle = cache_store.cache.store(LARGE_CONTENT)

        response = _client(cache_store, _user(BOB)).get(f"/cache/{handle}")

        assert response.status_code == 404
        assert response.json() == _expired_body(handle)

    def test_creator_is_served_the_content(self, cache_store: CacheStore) -> None:
        [handle] = cache_store.cache.store_batch([LARGE_CONTENT], owner=ALICE)

        response = _client(cache_store, _user(ALICE)).get(f"/cache/{handle}")

        assert response.status_code == 200
        body = response.json()
        assert body["content"] == LARGE_CONTENT[:200]
        assert body["total_pages"] == 3

    @pytest.mark.parametrize("role", [UserRole.NORMAL_USER, UserRole.ADMIN])
    def test_other_user_gets_the_missing_handle_answer(
        self, cache_store: CacheStore, role: UserRole
    ) -> None:
        [handle] = cache_store.cache.store_batch([LARGE_CONTENT], owner=ALICE)
        client = _client(cache_store, _user(BOB, role))

        response = client.get(f"/cache/{handle}")
        missing = client.get("/cache/00000000-0000-4000-8000-000000000000")

        assert response.status_code == missing.status_code == 404
        assert response.json() == _expired_body(handle)
        assert LARGE_CONTENT[:50] not in response.text

    @pytest.mark.parametrize("key", ["discovery:job-1", "temporal_query:job-2"])
    @pytest.mark.parametrize("role", [UserRole.NORMAL_USER, UserRole.ADMIN])
    def test_key_prefixed_entry_is_not_served(
        self, cache_store: CacheStore, key: str, role: UserRole
    ) -> None:
        cache_store.cache.store_with_key(key, LARGE_CONTENT)

        response = _client(cache_store, _user(BOB, role)).get(f"/cache/{key}")

        assert response.status_code == 404
        assert response.json() == _expired_body(key)
