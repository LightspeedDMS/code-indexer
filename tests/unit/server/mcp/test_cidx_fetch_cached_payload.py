"""Unit tests for cidx_fetch_cached_payload MCP handler (Issue #20).

Tests the discoverable cache fetch tool that lets MCP clients retrieve
full payloads when a search result is truncated and a cache_handle is
returned. Also tests that truncation messages in _truncate_xray_result
name this tool explicitly.

Mocking strategy:
- payload_cache: mocked via app_module.app.state
- User/permission: real User model
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_cidx_fetch_cached_payload

    return handle_cidx_fetch_cached_payload


# ---------------------------------------------------------------------------
# Tests: valid handle returns full payload
# ---------------------------------------------------------------------------


class TestCidxFetchCachedPayloadValidHandle:
    """Handler returns cached payload for a valid handle."""

    def test_valid_handle_returns_success_and_content(self):
        """Valid cache handle returns success=True and content."""
        user = _make_user(UserRole.NORMAL_USER)

        mock_result = MagicMock()
        mock_result.content = '{"matches": [{"file_path": "a.py"}]}'
        mock_result.page = 0
        mock_result.total_pages = 1
        mock_result.has_more = False

        mock_cache = MagicMock()
        mock_cache.retrieve.return_value = mock_result

        mock_app_state = MagicMock()
        mock_app_state.payload_cache = mock_cache

        mock_app = MagicMock()
        mock_app.state = mock_app_state

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        params = {"cache_handle": "abc123-uuid-handle"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("success") is True
        assert "content" in data

    def test_valid_handle_retrieve_called_with_handle(self):
        """Handler calls payload_cache.retrieve with the provided handle."""
        user = _make_user(UserRole.NORMAL_USER)

        mock_result = MagicMock()
        mock_result.content = "full content"
        mock_result.page = 0
        mock_result.total_pages = 1
        mock_result.has_more = False

        mock_cache = MagicMock()
        mock_cache.retrieve.return_value = mock_result

        mock_app = MagicMock()
        mock_app.state.payload_cache = mock_cache

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        params = {"cache_handle": "my-handle-xyz"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
        ):
            handler = _import_handler()
            handler(params, user)

        mock_cache.retrieve.assert_called_once()
        call_args = mock_cache.retrieve.call_args
        assert call_args[0][0] == "my-handle-xyz" or call_args.kwargs.get("handle") == "my-handle-xyz" or "my-handle-xyz" in str(call_args)


# ---------------------------------------------------------------------------
# Tests: invalid/expired handle
# ---------------------------------------------------------------------------


class TestCidxFetchCachedPayloadInvalidHandle:
    """Handler returns error for invalid or expired handles."""

    def test_expired_handle_returns_cache_expired_error(self):
        """Expired handle returns cache_expired error (not 500)."""
        from code_indexer.server.cache.payload_cache import CacheNotFoundError

        user = _make_user(UserRole.NORMAL_USER)

        mock_cache = MagicMock()
        mock_cache.retrieve.side_effect = CacheNotFoundError("Handle not found")

        mock_app = MagicMock()
        mock_app.state.payload_cache = mock_cache

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        params = {"cache_handle": "expired-handle-xyz"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("success") is False
        assert data.get("error") == "cache_expired"

    def test_missing_handle_returns_error(self):
        """Missing cache_handle parameter returns error."""
        user = _make_user(UserRole.NORMAL_USER)

        mock_app_module = MagicMock()
        mock_app_module.app.state.payload_cache = MagicMock()

        params = {}  # no cache_handle

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("success") is False


# ---------------------------------------------------------------------------
# Tests: auth
# ---------------------------------------------------------------------------


class TestCidxFetchCachedPayloadAuth:
    """Handler enforces auth and permission requirements."""

    def test_unauthenticated_request_rejected(self):
        """None user produces auth_required error."""
        handler = _import_handler()
        result = handler({"cache_handle": "h"}, None)
        data = _parse_response(result)
        assert data.get("error") == "auth_required"

    def test_missing_permission_rejected(self):
        """User without query_repos permission is rejected."""
        user = MagicMock(spec=User)
        user.username = "testuser"
        user.has_permission.return_value = False

        handler = _import_handler()
        result = handler({"cache_handle": "h"}, user)
        data = _parse_response(result)
        assert data.get("error") == "auth_required"


# ---------------------------------------------------------------------------
# Tests: truncation message names cidx_fetch_cached_payload (Issue #20)
# ---------------------------------------------------------------------------


class TestXrayTruncationMessageNamesTool:
    """_truncate_xray_result message tells user to use cidx_fetch_cached_payload."""

    def test_truncation_message_names_cidx_fetch_cached_payload(self):
        """When result is truncated, cache_handle message references cidx_fetch_cached_payload."""
        from code_indexer.server.mcp.handlers.xray import _truncate_xray_result

        # Build a large enough payload to trigger truncation
        large_matches = [
            {"file_path": f"file{i}.py", "line_number": i, "code_snippet": "x" * 200}
            for i in range(20)
        ]
        result = {
            "matches": large_matches,
            "evaluation_errors": [],
            "files_processed": 20,
            "files_total": 20,
            "elapsed_seconds": 1.0,
        }

        # Mock PayloadCache to return a truncated response
        mock_truncation = {
            "has_more": True,
            "preview": json.dumps({"matches": large_matches})[:200],
            "cache_handle": "test-uuid-1234",
            "total_size": 5000,
        }
        mock_cache = MagicMock()
        mock_cache.truncate_result.return_value = mock_truncation

        mock_app_state = MagicMock()
        mock_app_state.payload_cache = mock_cache

        mock_app = MagicMock()
        mock_app.state = mock_app_state

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
        ):
            truncated = _truncate_xray_result(result)

        # The truncation message must reference cidx_fetch_cached_payload
        truncated_str = json.dumps(truncated)
        assert "cidx_fetch_cached_payload" in truncated_str, (
            "Truncation response must reference the 'cidx_fetch_cached_payload' MCP tool "
            f"so users know how to fetch the full result. Got: {truncated_str[:500]}"
        )

    def test_truncation_message_included_in_truncated_result(self):
        """Truncated result includes a fetch_tool_hint field naming cidx_fetch_cached_payload."""
        from code_indexer.server.mcp.handlers.xray import _truncate_xray_result

        large_matches = [
            {"file_path": f"f{i}.py", "code_snippet": "y" * 200}
            for i in range(15)
        ]
        result = {
            "matches": large_matches,
            "evaluation_errors": [],
            "files_processed": 15,
            "files_total": 15,
            "elapsed_seconds": 0.5,
        }

        mock_truncation = {
            "has_more": True,
            "preview": "...",
            "cache_handle": "another-uuid",
            "total_size": 3000,
        }
        mock_cache = MagicMock()
        mock_cache.truncate_result.return_value = mock_truncation

        mock_app = MagicMock()
        mock_app.state.payload_cache = mock_cache

        mock_app_module = MagicMock()
        mock_app_module.app = mock_app

        with patch(
            "code_indexer.server.mcp.handlers.xray._utils.app_module", mock_app_module
        ):
            truncated = _truncate_xray_result(result)

        assert truncated.get("has_more") is True
        # Must include a tool hint field
        assert "fetch_tool_hint" in truncated, (
            "Truncated result must include 'fetch_tool_hint' field referencing "
            "cidx_fetch_cached_payload"
        )
        assert "cidx_fetch_cached_payload" in truncated["fetch_tool_hint"]


# ---------------------------------------------------------------------------
# Tests: tool registration
# ---------------------------------------------------------------------------


class TestCidxFetchCachedPayloadRegistration:
    """cidx_fetch_cached_payload is registered in HANDLER_REGISTRY."""

    def test_handler_registered_in_handler_registry(self):
        """HANDLER_REGISTRY includes cidx_fetch_cached_payload."""
        from code_indexer.server.mcp.handlers._legacy import HANDLER_REGISTRY

        assert "cidx_fetch_cached_payload" in HANDLER_REGISTRY, (
            "cidx_fetch_cached_payload must be registered in HANDLER_REGISTRY"
        )
