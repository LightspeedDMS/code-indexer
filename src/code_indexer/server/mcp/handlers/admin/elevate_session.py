"""MCP elevate_session handler (Story #925 AC3).

Exposes a single callable `elevate_session(args, user, session_key)` that
verifies a TOTP or recovery code and opens an elevation window via
ElevatedSessionManager.  All module-level names that must be patchable by
tests are imported at the top of this module so unittest.mock.patch can
replace them in the handler's namespace.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from code_indexer.server.auth.elevated_session_manager import elevated_session_manager
from code_indexer.server.auth.login_rate_limiter import login_rate_limiter
from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.auth.elevation_decorator import (
    _is_elevation_enforcement_enabled,
    _TOTP_SETUP_URL,
)
from code_indexer.server.web.mfa_routes import get_totp_service


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_elevate_args(args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return error dict for missing/ambiguous code, or None when args are valid."""
    has_totp = bool(args.get("totp_code"))
    has_recovery = bool(args.get("recovery_code"))
    if has_totp and has_recovery:
        return {
            "error": "ambiguous_code",
            "message": "Provide totp_code OR recovery_code, not both.",
        }
    if not has_totp and not has_recovery:
        return {
            "error": "missing_code",
            "message": "Provide totp_code or recovery_code.",
        }
    return None


def _verify_mcp_elevation_code(
    totp_svc: Any,
    username: str,
    args: Dict[str, Any],
    limiter_key: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Verify TOTP or recovery code.

    Returns (scope, None) on success or (None, error_dict) on failure.
    Records a rate-limiter failure on bad codes.
    """
    recovery_code = args.get("recovery_code")
    totp_code = args.get("totp_code")
    if recovery_code:
        if not totp_svc.verify_recovery_code(username, recovery_code, ip_address=None):
            login_rate_limiter.check_and_record_failure(limiter_key)
            return None, {
                "error": "elevation_failed",
                "message": "Invalid recovery code.",
            }
        return "totp_repair", None
    if not totp_svc.verify_enabled_code(username, totp_code):
        login_rate_limiter.check_and_record_failure(limiter_key)
        return None, {
            "error": "elevation_failed",
            "message": "Invalid or expired code.",
        }
    return "full", None


def _build_elevate_success_response(
    session_key: str, username: str, scope: str
) -> Dict[str, Any]:
    """Create elevation window and return success payload with float timestamps."""
    elevated_session_manager.create(
        session_key=session_key,
        username=username,
        elevated_from_ip=None,
        scope=scope,
    )
    session = elevated_session_manager.get_status(session_key)
    elevated_until = float(
        session.last_touched_at + elevated_session_manager._idle_timeout
    )
    max_until = float(session.elevated_at + elevated_session_manager._max_age)
    return {
        "elevated": True,
        "scope": scope,
        "elevated_until": elevated_until,
        "max_until": max_until,
    }


# ---------------------------------------------------------------------------
# Public handler
# ---------------------------------------------------------------------------


def elevate_session(
    args: Dict[str, Any], user: User, session_key: str = ""
) -> Dict[str, Any]:
    """Submit a TOTP or recovery code to open an MCP elevation window (Story #925 AC3)."""
    if not _is_elevation_enforcement_enabled():
        return {
            "error": "elevation_enforcement_disabled",
            "message": "Step-up elevation is currently disabled by the operator.",
        }

    arg_error = _validate_elevate_args(args)
    if arg_error is not None:
        return arg_error

    totp_svc = get_totp_service()
    if not totp_svc.is_mfa_enabled(user.username):
        return {
            "error": "totp_setup_required",
            "setup_url": _TOTP_SETUP_URL,
            "message": "Set up TOTP before performing this action.",
        }

    limiter_key = user.username
    is_locked, _ = login_rate_limiter.is_locked(limiter_key)
    if is_locked:
        return {
            "error": "rate_limited",
            "message": "Too many elevation attempts. Try again later.",
        }

    scope, code_error = _verify_mcp_elevation_code(
        totp_svc, user.username, args, limiter_key
    )
    if code_error is not None:
        return code_error

    if not session_key:
        return {
            "error": "missing_session_key",
            "message": "No session key on MCP request.",
        }

    login_rate_limiter.record_success(limiter_key)
    return _build_elevate_success_response(session_key, user.username, scope or "full")
