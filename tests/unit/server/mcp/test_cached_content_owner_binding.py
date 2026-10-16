"""MCP ``get_cached_content`` and ``cidx_fetch_cached_payload``: cached
content is served only to the user who created it.

An entry with no recorded creator, another user's entry and any
key-prefixed entry owned by another route (``discovery:...``,
``temporal_query:...``) answer ``cache_expired`` -- the answer a missing
handle gets. Admins get no bypass.

Real PayloadCache over every storage path (see ``_owner_bound_stores``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import _utils, xray_truncation
from code_indexer.server.mcp.handlers.search.cached_content import (
    handle_get_cached_content,
)
from code_indexer.server.mcp.handlers.xray import _cached_payload
from code_indexer.server.mcp.handlers.xray._cached_payload import (
    handle_cidx_fetch_cached_payload,
)
from tests.unit.server.cache._owner_bound_stores import (  # noqa: F401 (fixture)
    ALICE,
    BOB,
    LARGE_CONTENT,
    CacheStore,
    cache_store_fixture,
)

Door = Callable[[str, User], Dict[str, Any]]
MISSING = "00000000-0000-4000-8000-000000000000"


def _user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="unused",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


def _payload(response: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


def _get_cached_content(handle: str, user: User) -> Dict[str, Any]:
    return _payload(handle_get_cached_content({"handle": handle}, user))


def _fetch_cached_payload(handle: str, user: User) -> Dict[str, Any]:
    return _payload(handle_cidx_fetch_cached_payload({"cache_handle": handle}, user))


DOORS = [
    pytest.param(_get_cached_content, id="get_cached_content"),
    pytest.param(_fetch_cached_payload, id="cidx_fetch_cached_payload"),
]


@pytest.fixture(name="served_cache")
def served_cache_fixture(cache_store: CacheStore, monkeypatch: Any) -> CacheStore:
    """Expose the real cache as app.state.payload_cache to both doors."""
    app = SimpleNamespace(state=SimpleNamespace(payload_cache=cache_store.cache))
    monkeypatch.setattr(_utils, "app_module", SimpleNamespace(app=app))
    monkeypatch.setattr(_cached_payload, "_lazy_singleton_app_or_none", lambda: app)
    return cache_store


def _assert_expired(result: Dict[str, Any]) -> None:
    assert result["success"] is False
    assert result["error"] == "cache_expired"
    assert LARGE_CONTENT[:50] not in json.dumps(result)


@pytest.mark.parametrize("door", DOORS)
class TestMcpDoorsServeOnlyOwnedEntries:
    def test_creator_is_served(self, served_cache: CacheStore, door: Door) -> None:
        [handle] = served_cache.cache.store_batch([LARGE_CONTENT], owner=ALICE)

        result = door(handle, _user(ALICE))

        assert result["success"] is True
        assert result["content"] == LARGE_CONTENT[:200]
        assert result["total_pages"] == 3

    @pytest.mark.parametrize("role", [UserRole.NORMAL_USER, UserRole.ADMIN])
    def test_other_user_gets_cache_expired(
        self, served_cache: CacheStore, door: Door, role: UserRole
    ) -> None:
        [handle] = served_cache.cache.store_batch([LARGE_CONTENT], owner=ALICE)

        result = door(handle, _user(BOB, role))
        missing = door(MISSING, _user(BOB, role))

        _assert_expired(result)
        assert set(result) == set(missing)

    def test_entry_without_recorded_creator_is_not_served(
        self, served_cache: CacheStore, door: Door
    ) -> None:
        handle = served_cache.cache.store(LARGE_CONTENT)

        _assert_expired(door(handle, _user(BOB)))

    @pytest.mark.parametrize("key", ["discovery:job-1", "temporal_query:job-2"])
    @pytest.mark.parametrize("role", [UserRole.NORMAL_USER, UserRole.ADMIN])
    def test_key_prefixed_entry_is_not_served(
        self, served_cache: CacheStore, door: Door, key: str, role: UserRole
    ) -> None:
        served_cache.cache.store_with_key(key, LARGE_CONTENT)

        _assert_expired(door(key, _user(BOB, role)))


class TestSearchTruncationHandlesAreBoundToTheSearcher:
    @pytest.mark.parametrize(
        "mode,handle_key",
        [("semantic", "cache_handle"), ("fts", "snippet_cache_handle")],
    )
    def test_search_truncation_handle_served_to_searcher_only(
        self, served_cache: CacheStore, mode: str, handle_key: str
    ) -> None:
        from code_indexer.server.mcp.handlers.search._shared import (
            _apply_search_truncation,
        )

        results, _ = _apply_search_truncation(
            [{"code_snippet": LARGE_CONTENT}], mode, {}, owner=ALICE
        )
        handle = results[0][handle_key]
        assert handle

        for door in (_get_cached_content, _fetch_cached_payload):
            own = door(handle, _user(ALICE))
            assert own["success"] is True
            assert own["content"] == LARGE_CONTENT[:200]
            _assert_expired(door(handle, _user(BOB)))


class TestXrayPageSetIsBoundToCreator:
    def test_creator_fetches_xray_pages_other_user_refused(
        self, served_cache: CacheStore
    ) -> None:
        pages = [{"matches": [LARGE_CONTENT]}, {"matches": ["second"]}]
        manifest, total = xray_truncation.store_pages(
            served_cache.cache, ["matches"], pages, owner=ALICE
        )
        page_handles = json.loads(served_cache.cache.retrieve_full(manifest))[
            "page_handles"
        ]

        own = _fetch_cached_payload(manifest, _user(ALICE))
        assert own["success"] is True
        assert (own["page"], own["total_pages"]) == (1, total) == (1, 2)
        assert json.loads(own["content"]) == {"matches": [LARGE_CONTENT]}

        _assert_expired(_fetch_cached_payload(manifest, _user(BOB)))
        _assert_expired(_get_cached_content(page_handles[0], _user(BOB)))
        _assert_expired(_fetch_cached_payload(page_handles[0], _user(BOB)))
