"""Tests for Bug #1027: 1-indexed page numbers in cidx_fetch_cached_payload.

Root Cause: handle_cidx_fetch_cached_payload was previously 0-indexed at the
handler boundary, so callers who passed page=1 (expecting the first page) were
actually requesting page=1 internally, causing an off-by-one error.

Fix:
  - Handler normalises the incoming page to 1-indexed via max(1, int(page)).
  - Translates to 0-indexed before calling retrieve(): page - 1.
  - Returns 1-indexed page in the response: result.page + 1.
"""

import json
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeCacheResult:
    """Minimal stand-in for CacheRetrievalResult (returned by retrieve()).

    Uses a plain class rather than a dataclass to avoid decorator ambiguity
    in static-analysis tooling.
    """

    def __init__(
        self, content: str, page: int, total_pages: int, has_more: bool
    ) -> None:
        self.content = content
        self.page = page
        self.total_pages = total_pages
        self.has_more = has_more


def _make_user() -> MagicMock:
    user = MagicMock()
    user.has_permission.return_value = True
    return user


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_cidx_fetch_cached_payload

    return handle_cidx_fetch_cached_payload


def _parse_mcp_response(result: dict) -> dict:
    """Unpack the MCP content-array envelope into a plain dict.

    _mcp_response wraps data as:
        {"content": [{"type": "text", "text": "<JSON>"}]}
    """
    content = result["content"]
    assert len(content) == 1, "Expected exactly 1 content item in MCP response"
    parsed: dict = json.loads(content[0]["text"])
    return parsed


def _make_cache(page: int = 0, total_pages: int = 3) -> MagicMock:
    """Return a mock payload_cache whose retrieve() returns a FakeCacheResult."""
    cache = MagicMock()
    cache.retrieve.return_value = FakeCacheResult(
        content="test content for page",
        page=page,
        total_pages=total_pages,
        has_more=(page < total_pages - 1),
    )
    return cache


def _call_handler(cache: MagicMock, page=None) -> dict:
    """Invoke the handler with a fake payload_cache and optional page."""
    handler = _import_handler()
    user = _make_user()
    params: dict = {"cache_handle": "test-handle-abc"}
    if page is not None:
        params["page"] = page

    with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app_module:
        mock_app_module.app.state.payload_cache = cache
        raw = handler(params, user)

    return _parse_mcp_response(raw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCachePageIndexBug1027:
    """Handler must accept 1-indexed pages and translate to 0-indexed internally."""

    def test_page_1_calls_retrieve_with_zero_index(self) -> None:
        """Passing page=1 must translate to retrieve(page=0)."""
        cache = _make_cache(page=0, total_pages=3)
        data = _call_handler(cache, page=1)

        assert data["success"] is True
        cache.retrieve.assert_called_once_with("test-handle-abc", page=0)

    def test_default_page_calls_retrieve_with_zero_index(self) -> None:
        """Omitting page must default to 1 (1-indexed) -> retrieve(page=0)."""
        cache = _make_cache(page=0, total_pages=3)
        data = _call_handler(cache)

        assert data["success"] is True
        cache.retrieve.assert_called_once_with("test-handle-abc", page=0)

    def test_page_zero_clamped_to_first_page(self) -> None:
        """page=0 must be clamped to 1 via max(1, ...), so retrieve gets page=0."""
        cache = _make_cache(page=0, total_pages=3)
        data = _call_handler(cache, page=0)

        assert data["success"] is True
        cache.retrieve.assert_called_once_with("test-handle-abc", page=0)

    def test_response_page_is_one_indexed(self) -> None:
        """Handler must return result.page + 1 so callers see 1-indexed page numbers."""
        cache = _make_cache(page=0, total_pages=3)
        data = _call_handler(cache, page=1)

        assert data["success"] is True
        assert data["page"] == 1, (
            f"Bug #1027: response 'page' must be 1-indexed; got {data['page']}"
        )

    def test_page_2_calls_retrieve_with_index_one(self) -> None:
        """Passing page=2 must translate to retrieve(page=1) and response page=2."""
        cache = _make_cache(page=1, total_pages=3)
        data = _call_handler(cache, page=2)

        assert data["success"] is True
        cache.retrieve.assert_called_once_with("test-handle-abc", page=1)
        assert data["page"] == 2

    def test_cache_not_found_returns_cache_expired_error(self) -> None:
        """When retrieve raises CacheNotFoundError, response must be cache_expired."""
        from code_indexer.server.cache.payload_cache import CacheNotFoundError

        cache = MagicMock()
        cache.retrieve.side_effect = CacheNotFoundError("handle expired")

        data = _call_handler(cache, page=1)

        assert data["success"] is False
        assert data["error"] == "cache_expired", (
            f"Expected error='cache_expired', got '{data['error']}'"
        )

    def test_missing_cache_handle_returns_error(self) -> None:
        """When cache_handle is not provided, handler must return missing_handle."""
        handler = _import_handler()
        user = _make_user()
        cache = MagicMock()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.payload_cache = cache
            raw = handler({}, user)

        data = _parse_mcp_response(raw)
        assert data["success"] is False
        assert data["error"] == "missing_handle"

    def test_none_payload_cache_returns_cache_unavailable(self) -> None:
        """When app.state.payload_cache is None, return cache_unavailable."""
        handler = _import_handler()
        user = _make_user()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.payload_cache = None
            raw = handler({"cache_handle": "some-handle"}, user)

        data = _parse_mcp_response(raw)
        assert data["success"] is False
        assert data["error"] == "cache_unavailable"
