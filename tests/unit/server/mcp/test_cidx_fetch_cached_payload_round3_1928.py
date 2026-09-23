"""Bug #1928 round 3 (P2 -- Codex): cidx_fetch_cached_payload's `page`
parameter must be a real int >= 1 with NO clamping/coercion -- a
float, zero, or negative value returns a structured `invalid_page`
error, never silently coerced/clamped to 1. A handle carrying the
pages-v1 prefix whose content fails strict manifest validation
(MalformedManifestError) returns a structured `malformed_cache_entry`
error, not a generic stringified-exception fallback.

Bug #1928 final round (Codex P1): this strict, no-clamp contract is
scoped to pages-v1 (xray-pv1-*) handles ONLY -- a legacy handle keeps
its pre-#1928 lenient max(1, int(page or 1)) coercion (see
test_bug_1027_cache_page_index.py /
test_payload_cache_page_index_bug.py). The float/zero/negative cases
below now use a pages-v1-prefixed handle so they genuinely exercise
the strict path. The former "string page" case moved to
test_cidx_fetch_cached_payload_pv1_page_validation_1928.py as an
ACCEPTANCE test instead (Opus P3.3: a decimal-digit string like "2" is
coerced, not rejected, for pv1 handles) -- bool rejection is also
covered there.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import xray_truncation as xt


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


# Bug #1928 final round (Codex P1): strict, no-clamp page validation is a
# pages-v1 (xray-pv1-*) handle-only contract -- these tests must use a
# handle carrying that prefix, never a bare/legacy-looking handle, or
# they would incorrectly assert strict behavior on what is actually the
# legacy coercion path.
_PV1_HANDLE = f"{xt._PAGES_V1_HANDLE_PREFIX}invalid-page-test-dummy"


class TestInvalidPageNoClampOrCoercion:
    def test_float_page_returns_invalid_page_error(self) -> None:
        data = _call({"cache_handle": _PV1_HANDLE, "page": 2.5}, MagicMock())

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"

    def test_zero_page_returns_invalid_page_error_not_clamped(self) -> None:
        data = _call({"cache_handle": _PV1_HANDLE, "page": 0}, MagicMock())

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"

    def test_negative_page_returns_invalid_page_error(self) -> None:
        data = _call({"cache_handle": _PV1_HANDLE, "page": -3}, MagicMock())

        assert data.get("success") is False
        assert data.get("error") == "invalid_page"


class TestMalformedCacheEntryStructuredError:
    def test_malformed_manifest_returns_structured_error(self, tmp_path) -> None:
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(preview_size_chars=300, max_fetch_size_chars=300)
        cache = PayloadCache(db_path=tmp_path / "payload_cache.db", config=config)
        try:
            cache.initialize()
            bad_handle = f"{xt._PAGES_V1_HANDLE_PREFIX}corrupt"
            cache.store_with_key(bad_handle, "not even json")

            data = _call({"cache_handle": bad_handle, "page": 1}, cache)
        finally:
            cache.close()

        assert data.get("success") is False
        assert data.get("error") == "malformed_cache_entry"
