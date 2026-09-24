"""Bug #1928 final round (Codex P1 / Opus P3.3): cidx_fetch_cached_payload's
strict, no-clamp `page` validation is a contract for pages-v1
(xray-pv1-*) handles ONLY.

Codex P1: round 3 applied strict validation to EVERY handle, including
legacy (pre-#1928) ones -- breaking their pre-existing page=0 -> 1
clamp/coercion, a change outside #1928's scope
(test_bug_1027_cache_page_index.py /
test_payload_cache_page_index_bug.py restore that legacy contract).

Opus P3.3: for pages-v1 handles specifically, a decimal-digit STRING
(e.g. "2") is accepted and coerced to int -- it is not silently
"clamping" (which would mean forcing an out-of-range/invalid value to a
valid one); it is ordinary numeric-string parsing of an
otherwise-valid value. Zero, negative integers, floats, and bools are
still rejected outright, with no clamping.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers import xray_truncation as xt

_PV1_DUMMY_HANDLE = f"{xt._PAGES_V1_HANDLE_PREFIX}bool-reject-dummy"


def _make_user() -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_cidx_fetch_cached_payload

    return handle_cidx_fetch_cached_payload


def _call(params: Dict[str, Any], cache: Any) -> Dict[str, Any]:
    mock_app = MagicMock()
    mock_app.state.payload_cache = cache
    mock_app_module = MagicMock()
    mock_app_module.app = mock_app

    with patch(
        "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
    ):
        handler = _import_handler()
        result = handler(params, _make_user())
    return _parse_response(result)


@pytest.fixture
def real_pv1_cache(tmp_path: Path):
    """A real on-disk PayloadCache holding one genuine pages-v1 page-set
    (2 pages), so acceptance cases exercise a real fetch, not just the
    validation gate."""
    config = PayloadCacheConfig(preview_size_chars=300, max_fetch_size_chars=300)
    cache = PayloadCache(db_path=tmp_path / "pv1.db", config=config)
    cache.initialize()
    try:
        handle, total_pages = xt.store_pages(
            cache,
            ["items"],
            [{"items": ["page-one-content"]}, {"items": ["page-two-content"]}],
        )
        yield cache, handle, total_pages
    finally:
        cache.close()


class TestPv1PageRejectsBoolsNoClamp:
    def test_true_page_returns_invalid_page_error(self) -> None:
        data = _call({"cache_handle": _PV1_DUMMY_HANDLE, "page": True}, MagicMock())

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"

    def test_false_page_returns_invalid_page_error(self) -> None:
        data = _call({"cache_handle": _PV1_DUMMY_HANDLE, "page": False}, MagicMock())

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"


class TestPv1PageAcceptsDecimalDigitString:
    def test_digit_string_page_is_coerced_and_succeeds(self, real_pv1_cache) -> None:
        cache, handle, _total_pages = real_pv1_cache

        data = _call({"cache_handle": handle, "page": "2"}, cache)

        assert data.get("success") is True, f"Expected success but got: {data}"
        assert data.get("page") == 2
        assert "page-two-content" in data.get("content", "")

    def test_non_digit_string_page_still_rejected(self, real_pv1_cache) -> None:
        cache, handle, _total_pages = real_pv1_cache

        data = _call({"cache_handle": handle, "page": "abc"}, cache)

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"

    def test_negative_digit_like_string_still_rejected(self, real_pv1_cache) -> None:
        cache, handle, _total_pages = real_pv1_cache

        data = _call({"cache_handle": handle, "page": "-2"}, cache)

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"

    def test_zero_string_page_still_rejected(self, real_pv1_cache) -> None:
        cache, handle, _total_pages = real_pv1_cache

        data = _call({"cache_handle": handle, "page": "0"}, cache)

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"
