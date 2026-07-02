"""v10.4.5 tests for Defect 1: set_session_impersonation error surface improvements.

Root cause identified:
- When session_state is None the handler silently returns {"status": "ok"}
  while doing nothing — misleading callers into thinking impersonation succeeded.
- bare except Exception returns str(e) which loses the exception class name.

Post-fix behaviour asserted exactly:
- session_state=None + username -> {"status":"error","error":"session_state_unavailable","message":"<non-empty>"}
- user not found           -> {"status":"error","error":"User not found: ghost@example.com"}
- unexpected exception     -> {"status":"error","error":"RuntimeError: DB connection lost"}
- non-admin user           -> {"status":"error","error":"Impersonation requires ADMIN role"}
- elevation gate fired     -> {"error":"elevation_required","message":"<non-empty>"}

Shared helpers declared:
- _make_user(username, role) — single parameterised factory
- _make_session_state(session_id) — mock MCPSessionState
- _parse_response(result) — unwrap MCP envelope; json.loads returns Any so a
  cast(Dict[str, Any], ...) is applied — dict shape is guaranteed by the protocol
- _call_handler(...) — invoke handler with elevation disabled and controlled mock
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user(
    username: str = "admin@example.com", role: UserRole = UserRole.ADMIN
) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make_session_state(session_id: str = "sess-abc123") -> MagicMock:
    ss = MagicMock()
    ss.session_id = session_id
    ss.is_impersonating = False
    return ss


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    # json.loads returns Any; cast required so mypy can verify the declared return type.
    # Dict shape is guaranteed by the MCP protocol envelope (content[0]["text"] is always
    # a JSON-serialized dict from _mcp_response).
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _call_handler(
    args: Dict[str, Any],
    user: User,
    session_state: Any = None,
    get_user_return: Any = None,
    get_user_side_effect: Any = None,
) -> Dict[str, Any]:
    """Invoke handle_set_session_impersonation with elevation kill-switch OFF
    and a controlled user_manager mock. Returns parsed response dict."""
    from code_indexer.server.mcp.handlers.admin import handle_set_session_impersonation

    mock_app = MagicMock()
    if get_user_side_effect is not None:
        mock_app.user_manager.get_user.side_effect = get_user_side_effect
    else:
        mock_app.user_manager.get_user.return_value = get_user_return

    with (
        patch(
            "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled",
            return_value=False,
        ),
        patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
        patch("code_indexer.server.mcp.handlers.admin._utils.app_module", mock_app),
    ):
        result = handle_set_session_impersonation(
            args, user, session_state=session_state
        )

    return _parse_response(result)


# ---------------------------------------------------------------------------
# AC1: session_state=None with username -> explicit error code + message
# ---------------------------------------------------------------------------


def test_session_state_none_returns_explicit_error():
    """session_state=None + username provided -> error 'session_state_unavailable' with message.

    The old code silently returned {"status": "ok"} while doing nothing.
    The fix returns an explicit error so callers know impersonation did not take effect.
    """
    admin = _make_user()
    target = _make_user("target@example.com", UserRole.NORMAL_USER)

    data = _call_handler(
        args={"username": "target@example.com"},
        user=admin,
        session_state=None,
        get_user_return=target,
    )

    assert data.get("status") == "error"
    assert data.get("error") == "session_state_unavailable"
    assert len(data.get("message", "")) > 0


# ---------------------------------------------------------------------------
# AC2: user not found -> exact error string naming the username
# ---------------------------------------------------------------------------


def test_user_not_found_returns_specific_error():
    """get_user returns None -> exact error 'User not found: ghost@example.com'.

    Pins the existing error format so regressions are caught.
    """
    admin = _make_user()

    data = _call_handler(
        args={"username": "ghost@example.com"},
        user=admin,
        session_state=_make_session_state(),
        get_user_return=None,
    )

    assert data.get("status") == "error"
    assert data.get("error") == "User not found: ghost@example.com"


# ---------------------------------------------------------------------------
# AC3: unexpected exception -> exact class + message in error string
# ---------------------------------------------------------------------------


def test_internal_exception_does_not_mask_root_cause():
    """get_user raises RuntimeError("DB connection lost") -> error == 'RuntimeError: DB connection lost'.

    The old code returned str(e) == 'DB connection lost' which loses the class name.
    The fix returns f'{type(e).__name__}: {e}' so operators can diagnose the exception type.
    """
    admin = _make_user()

    data = _call_handler(
        args={"username": "target@example.com"},
        user=admin,
        session_state=_make_session_state(),
        get_user_side_effect=RuntimeError("DB connection lost"),
    )

    assert data.get("status") == "error"
    assert data.get("error") == "RuntimeError: DB connection lost"


# ---------------------------------------------------------------------------
# AC4: non-admin user -> exact error message
# ---------------------------------------------------------------------------


def test_admin_role_check_works():
    """Non-admin user -> exactly 'Impersonation requires ADMIN role'."""
    normal_user = _make_user("user@example.com", UserRole.NORMAL_USER)

    data = _call_handler(
        args={"username": "someone@example.com"},
        user=normal_user,
        session_state=_make_session_state(),
    )

    assert data.get("status") == "error"
    assert data.get("error") == "Impersonation requires ADMIN role"


# ---------------------------------------------------------------------------
# AC5: elevation gate fires -> elevation_required with actionable message
# ---------------------------------------------------------------------------


def test_elevation_required_path_returns_actionable_error():
    """Elevation ON + no active window -> error='elevation_required', message non-empty.

    The elevation decorator (elevation_decorator.py Gate 6) returns this when
    touch_atomic_for_user returns None (no active window).
    """
    from code_indexer.server.mcp.handlers.admin import handle_set_session_impersonation

    admin = _make_user()
    mock_esm = MagicMock()
    mock_esm.touch_atomic_for_user.return_value = None

    mock_totp = MagicMock()
    mock_totp.is_mfa_enabled.return_value = True

    with (
        patch(
            "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled",
            return_value=True,
        ),
        patch(
            "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager",
            mock_esm,
        ),
        patch(
            "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service",
            return_value=mock_totp,
        ),
    ):
        result = handle_set_session_impersonation(
            {"username": "target@example.com"},
            admin,
            session_key="test-session-key-999",
        )

    # v10.4.6: the elevation decorator now wraps error responses via _mcp_response
    # (closing Open 8 root cause where raw dicts were stringified at the MCP
    # transport layer as "Error occurred during tool execution"). Parse the MCP
    # envelope to access the underlying structured error.
    import json as _json

    assert "content" in result
    payload = _json.loads(result["content"][0]["text"])
    assert payload.get("error") == "elevation_required"
    assert len(payload.get("message", "")) > 0
