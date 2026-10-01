"""Bug #1928 (Codex, post-final-round): a falsey/non-string `cache_handle`
(None, False, 0, []) reaches `cache_handle.startswith(...)` BEFORE HEAD's
`missing_handle` check, raising an uncaught AttributeError instead of the
structured `missing_handle` response HEAD always returned for a falsy
handle.

Fix: do HEAD's missing-handle check FIRST (`if not cache_handle`), then
detect the pages-v1 prefix via `isinstance(cache_handle, str) and
cache_handle.startswith(...)` -- so a falsy value never reaches
`.startswith()` at all, and a TRUTHY non-string value (matching HEAD,
which never isinstance-checked either) falls through to the legacy path
exactly as it always did.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


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


def _call(params: Dict[str, Any]) -> Dict[str, Any]:
    mock_app = MagicMock()
    mock_app.state.payload_cache = MagicMock()
    mock_app_module = MagicMock()
    mock_app_module.app = mock_app

    with patch(
        "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
    ):
        handler = _import_handler()
        result = handler(params, _make_user())
    return _parse_response(result)


class TestFalsyCacheHandleReturnsMissingHandle:
    """Every falsy value the `cache_handle` key can legally carry must hit
    the SAME missing_handle response as an absent key -- never an
    uncaught AttributeError from .startswith() on a non-str value."""

    @pytest.mark.parametrize("falsy_handle", [None, False, 0, []])
    def test_falsy_cache_handle_returns_missing_handle(self, falsy_handle) -> None:
        data = _call({"cache_handle": falsy_handle, "page": 1})

        assert data.get("success") is False
        assert data.get("error") == "missing_handle", (
            f"falsy cache_handle {falsy_handle!r} must return missing_handle, "
            f"got: {data}"
        )

    def test_empty_string_cache_handle_returns_missing_handle(self) -> None:
        data = _call({"cache_handle": "", "page": 1})

        assert data.get("success") is False
        assert data.get("error") == "missing_handle"

    def test_absent_cache_handle_key_returns_missing_handle(self) -> None:
        data = _call({"page": 1})

        assert data.get("success") is False
        assert data.get("error") == "missing_handle"
