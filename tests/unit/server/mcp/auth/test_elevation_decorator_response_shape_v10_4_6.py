"""v10.4.6 tests: elevation decorator error helpers must return MCP-wrapped shape.

Root cause (Defect 1, Open 8):
  _disabled_error(), _elevation_required_error(), _totp_setup_required_error()
  returned raw dicts. The MCP protocol layer expects:
    {"content": [{"type": "text", "text": "<json-stringified-payload>"}]}
  The mismatch caused the MCP client to surface generic
  "Error occurred during tool execution" instead of structured error codes.

Fix (v10.4.6): each helper wraps its payload via _mcp_response() from
  code_indexer.server.mcp.handlers._utils.

Tests drive the decorator wrapper directly — no external server, no HTTP.
Patches follow the established seam pattern from test_elevation_decorator_gate1_kwargs.py.
"""

from __future__ import annotations

import contextlib
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation

# ---------------------------------------------------------------------------
# Patch targets (same seams as existing elevation decorator tests)
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_USERNAME = "admin"
_SESSION_KEY = "jti-test-shape-v10-4-6-001"
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_IDLE = 300
_MAX_AGE = 1800


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_all(esm, totp_svc, enforcement=True):
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_ESM_PATH, esm),
        patch(_TOTP_PATH, return_value=totp_svc),
    ):
        yield


def _parse_mcp_payload(result: dict) -> dict:
    """Assert result has MCP shape and return the parsed inner JSON payload.

    MCP-compliant shape:
      {"content": [{"type": "text", "text": "<json>"}]}

    Returns the parsed dict from content[0]["text"].
    Raises AssertionError with a descriptive message on shape violation.
    """
    assert "content" in result, (
        f"MCP response must have 'content' key; got keys: {list(result.keys())!r}"
    )
    content = result["content"]
    assert isinstance(content, list) and len(content) >= 1, (
        f"content must be a non-empty list, got: {content!r}"
    )
    item = content[0]
    assert item.get("type") == "text", (
        f"content[0].type must be 'text', got: {item.get('type')!r}"
    )
    text = item.get("text", "")
    payload = json.loads(text)
    assert isinstance(payload, dict), (
        f"Parsed text must be a dict, got {type(payload).__name__!r}"
    )
    return payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    return User(
        username=_USERNAME,
        role="admin",
        password_hash=_DUMMY_HASH,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def manager(tmp_path):
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elev_shape_v10_4_6.db"),
    )


@pytest.fixture
def totp_enabled():
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    return svc


def _stub_handler(args, user, **kwargs):
    """Minimal pass-through handler returning a plain dict."""
    return {"success": True, "called": True}


# ---------------------------------------------------------------------------
# Test: Gate 2 — esm is None → _disabled_error fires → must be MCP-wrapped
# ---------------------------------------------------------------------------


class TestDisabledErrorMcpShape:
    """_disabled_error() via Gate 2 (esm=None) must return MCP-wrapped shape."""

    def test_disabled_error_returns_mcp_content_shape(self, admin_user, tmp_path):
        """When elevated_session_manager is None (Gate 2), the decorator returns
        an MCP-wrapped disabled error — NOT a raw dict.

        The response must have shape:
          {"content": [{"type": "text", "text": "<json>"}]}
        and the parsed JSON must be:
          {"error": "elevation_enforcement_disabled", "message": "..."}
        """
        totp_svc = MagicMock()
        totp_svc.is_mfa_enabled.return_value = True

        decorated = require_mcp_elevation()(_stub_handler)

        with (
            patch(_ENFORCEMENT_PATH, return_value=True),
            patch(_ESM_PATH, None),  # Gate 2: esm is None
            patch(_TOTP_PATH, return_value=totp_svc),
        ):
            result = decorated({}, admin_user, session_key=_SESSION_KEY)

        payload = _parse_mcp_payload(result)
        assert payload.get("error") == "elevation_enforcement_disabled", (
            f"Expected error='elevation_enforcement_disabled', got payload: {payload!r}"
        )
        assert "message" in payload, (
            f"Payload must include 'message' key, got: {payload!r}"
        )


# ---------------------------------------------------------------------------
# Test: Gate 4 — TOTP not set up → _totp_setup_required_error fires → MCP-wrapped
# ---------------------------------------------------------------------------


class TestTotpSetupRequiredErrorMcpShape:
    """_totp_setup_required_error() via Gate 4 must return MCP-wrapped shape."""

    def test_totp_setup_required_error_returns_mcp_content_shape(
        self, admin_user, manager
    ):
        """When is_mfa_enabled returns False (Gate 4), the decorator returns an
        MCP-wrapped totp_setup_required error — NOT a raw dict.

        Parsed JSON must have:
          error == "totp_setup_required"
          setup_url == "/admin/mfa/setup"
          message is present
        """
        totp_svc = MagicMock()
        totp_svc.is_mfa_enabled.return_value = False  # Gate 4 fires

        decorated = require_mcp_elevation()(_stub_handler)

        with _patch_all(manager, totp_svc, enforcement=True):
            result = decorated({}, admin_user, session_key=_SESSION_KEY)

        payload = _parse_mcp_payload(result)
        assert payload.get("error") == "totp_setup_required", (
            f"Expected error='totp_setup_required', got payload: {payload!r}"
        )
        assert payload.get("setup_url") == "/admin/mfa/setup", (
            f"Expected setup_url='/admin/mfa/setup', got: {payload.get('setup_url')!r}"
        )
        assert "message" in payload, (
            f"Payload must include 'message' key, got: {payload!r}"
        )


# ---------------------------------------------------------------------------
# Test: Gate 6 — no active elevation window → _elevation_required_error → MCP-wrapped
# ---------------------------------------------------------------------------


class TestElevationRequiredErrorMcpShape:
    """_elevation_required_error() via Gate 6 must return MCP-wrapped shape."""

    def test_elevation_required_error_returns_mcp_content_shape(
        self, admin_user, manager, totp_enabled
    ):
        """When no active elevation window exists (Gate 6 fails), the decorator
        returns an MCP-wrapped elevation_required error — NOT a raw dict.

        The response must have shape:
          {"content": [{"type": "text", "text": "<json>"}]}
        and the parsed JSON must be:
          {"error": "elevation_required", "message": "..."}
        """
        # manager has no active elevation window for _SESSION_KEY
        decorated = require_mcp_elevation()(_stub_handler)

        with _patch_all(manager, totp_enabled, enforcement=True):
            result = decorated({}, admin_user, session_key=_SESSION_KEY)

        payload = _parse_mcp_payload(result)
        assert payload.get("error") == "elevation_required", (
            f"Expected error='elevation_required', got payload: {payload!r}"
        )
        assert "message" in payload, (
            f"Payload must include 'message' key, got: {payload!r}"
        )


# ---------------------------------------------------------------------------
# Test: all gates pass → handler return is passed through unchanged
# ---------------------------------------------------------------------------


class TestHandlerPassThroughUnchanged:
    """When all gates pass, the handler return value is returned as-is.

    The handler in these tests already returns a plain dict (not MCP-wrapped).
    The decorator must NOT double-wrap handler output.
    """

    def test_handler_returns_pass_through_unchanged(
        self, admin_user, manager, totp_enabled
    ):
        """When all gates pass, the decorator returns the handler's dict as-is
        without wrapping it in MCP content shape.

        This verifies no double-wrapping occurs when the handler itself returns
        a plain dict (e.g. {"success": True, "called": True}).
        """
        # Create active elevation window so Gate 6 passes
        manager.create(
            session_key=_SESSION_KEY,
            username=_USERNAME,
            elevated_from_ip="127.0.0.1",
            scope="full",
        )
        decorated = require_mcp_elevation()(_stub_handler)

        with _patch_all(manager, totp_enabled, enforcement=True):
            result = decorated({}, admin_user, session_key=_SESSION_KEY)

        # Handler returns {"success": True, "called": True} — passed through as-is
        assert result.get("called") is True, (
            f"Handler must be invoked and its return passed through, got: {result!r}"
        )
        assert result.get("success") is True, (
            f"Expected success=True in pass-through result, got: {result!r}"
        )
        # No MCP wrapping on pass-through — handler is responsible for its own shape
        assert "content" not in result, (
            f"Decorator must NOT wrap handler output; got unexpected 'content' key in: {result!r}"
        )
