"""Tests for Bug #1027: Payload cache page-index error on small xray results.

Small payloads (~3KB, single page) produce "Page 1 out of range for handle X
(total: 1)" error when the caller uses 1-indexed pages (page=1 meaning "first
page") but retrieve() is 0-indexed internally.

The fix is: cidx_fetch_cached_payload must translate from the 1-indexed
external API to the 0-indexed internal retrieve() call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.cache.payload_cache import (
    PayloadCache,
    PayloadCacheConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse(result: Dict[str, Any]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return parsed


@pytest.fixture
def cache(tmp_path: Path) -> PayloadCache:
    """Real PayloadCache with max_fetch_size_chars=5000 (default)."""
    config = PayloadCacheConfig(
        preview_size_chars=2000,
        max_fetch_size_chars=5000,
    )
    c = PayloadCache(db_path=tmp_path / "bug1027.db", config=config)
    c.initialize()
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Bug reproduction: page=1 on a single-page result must succeed
# ---------------------------------------------------------------------------


class TestSmallPayloadPageIndexBug:
    """Bug #1027: page=1 from 1-indexed caller fails on single-page payloads."""

    def test_small_payload_page_1_is_first_page(self, cache: PayloadCache) -> None:
        """Caller uses page=1 (1-indexed) for a 3KB single-page result — must succeed.

        Before the fix: retrieve(handle, page=1) raises CacheNotFoundError
        because 1 >= total_pages(1).
        After the fix: handle_cidx_fetch_cached_payload translates page=1 to
        internal page=0 and returns the full content.
        """
        from code_indexer.server.mcp.handlers.xray import (
            handle_cidx_fetch_cached_payload,
        )

        # Store a ~3KB payload (> preview_size_chars=2000, < max_fetch_size=5000)
        small_payload = "X" * 3000
        handle = cache.store(small_payload)

        mock_app_module = MagicMock()
        mock_app_module.app.state.payload_cache = cache

        params = {"cache_handle": handle, "page": 1}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app_module,
        ):
            result = handle_cidx_fetch_cached_payload(params, _make_user())

        data = _parse(result)
        assert data["success"] is True, f"Expected success but got: {data}"
        assert data["content"] == small_payload
        assert data["has_more"] is False
        assert data["total_pages"] == 1

    def test_small_payload_page_0_still_works(self, cache: PayloadCache) -> None:
        """Caller using page=0 (default/legacy) on a single-page result still succeeds."""
        from code_indexer.server.mcp.handlers.xray import (
            handle_cidx_fetch_cached_payload,
        )

        small_payload = "Y" * 2500
        handle = cache.store(small_payload)

        mock_app_module = MagicMock()
        mock_app_module.app.state.payload_cache = cache

        params = {"cache_handle": handle, "page": 0}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app_module,
        ):
            result = handle_cidx_fetch_cached_payload(params, _make_user())

        data = _parse(result)
        assert data["success"] is True, f"Expected success but got: {data}"
        assert data["content"] == small_payload

    def test_multi_page_payload_page_1_returns_first_page(
        self, cache: PayloadCache
    ) -> None:
        """Multi-page payload: page=1 (1-indexed) returns first 5000 chars."""
        from code_indexer.server.mcp.handlers.xray import (
            handle_cidx_fetch_cached_payload,
        )

        first_page = "A" * 5000
        second_page = "B" * 5000
        large_payload = first_page + second_page
        handle = cache.store(large_payload)

        mock_app_module = MagicMock()
        mock_app_module.app.state.payload_cache = cache

        params = {"cache_handle": handle, "page": 1}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app_module,
        ):
            result = handle_cidx_fetch_cached_payload(params, _make_user())

        data = _parse(result)
        assert data["success"] is True
        assert data["content"] == first_page
        assert data["has_more"] is True
        assert data["total_pages"] == 2

    def test_multi_page_payload_page_2_returns_second_page(
        self, cache: PayloadCache
    ) -> None:
        """Multi-page payload: page=2 (1-indexed) returns second chunk."""
        from code_indexer.server.mcp.handlers.xray import (
            handle_cidx_fetch_cached_payload,
        )

        first_page = "A" * 5000
        second_page = "B" * 5000
        large_payload = first_page + second_page
        handle = cache.store(large_payload)

        mock_app_module = MagicMock()
        mock_app_module.app.state.payload_cache = cache

        params = {"cache_handle": handle, "page": 2}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app_module,
        ):
            result = handle_cidx_fetch_cached_payload(params, _make_user())

        data = _parse(result)
        assert data["success"] is True
        assert data["content"] == second_page
        assert data["has_more"] is False
        assert data["total_pages"] == 2

    def test_truly_out_of_range_page_still_errors(self, cache: PayloadCache) -> None:
        """Page beyond actual range still raises cache_expired error."""
        from code_indexer.server.mcp.handlers.xray import (
            handle_cidx_fetch_cached_payload,
        )

        small_payload = "Z" * 3000
        handle = cache.store(small_payload)

        mock_app_module = MagicMock()
        mock_app_module.app.state.payload_cache = cache

        # page=2 for a single-page result: 1-indexed page 2 means internal page 1
        # total_pages=1, internal page 1 >= 1 -> out of range
        params = {"cache_handle": handle, "page": 2}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module",
            mock_app_module,
        ):
            result = handle_cidx_fetch_cached_payload(params, _make_user())

        data = _parse(result)
        assert data["success"] is False
        assert data["error"] == "cache_expired"
