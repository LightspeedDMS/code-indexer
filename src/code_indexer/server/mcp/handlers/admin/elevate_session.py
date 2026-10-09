"""MCP elevate_session handler (Story #925 AC3).

Exposes a single callable `elevate_session(args, user, session_key)` that
verifies a TOTP or recovery code and opens an elevation window via
ElevatedSessionManager.  It is the MCP twin of REST `POST /auth/elevate` and
follows the same rules: any authenticated user with TOTP enrolled may open a
window for their own username, keyed by the session key of the credential
that made the call.  All module-level names that must be patchable by
tests are imported at the top of this module so unittest.mock.patch can
replace them in the handler's namespace.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Request

from code_indexer.server.auth.dependencies import _mfa_setup_url_for_role
from code_indexer.server.auth.elevated_session_manager import elevated_session_manager
from code_indexer.server.auth.elevation_step_up import (
    StepUpOutcome,
    StepUpResult,
    step_up,
)
from code_indexer.server.auth.login_rate_limiter import login_rate_limiter
from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.auth.elevation_decorator import (
    _is_elevation_enforcement_enabled,
)
from code_indexer.server.mcp.handlers._utils import _mcp_response
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


def _client_ip(http_request: Optional[Request]) -> str:
    """Client IP exactly as REST /auth/elevate derives it."""
    if http_request is not None and http_request.client:
        return str(http_request.client.host)
    return "unknown"


def _step_up_payload(result: StepUpResult) -> Dict[str, Any]:
    """This door's payload for a step-up result (same shapes as REST)."""
    if result.outcome is StepUpOutcome.BUSY:
        return {
            "error": "busy",
            "message": "Elevation is busy, try again shortly.",
        }
    if result.outcome is StepUpOutcome.LOCKED_OUT:
        return {
            "error": "rate_limited",
            "message": "Too many elevation attempts. Try again later.",
        }
    if result.outcome is StepUpOutcome.INVALID_CODE:
        return {
            "error": "elevation_failed",
            "message": (
                "Invalid recovery code."
                if result.used_recovery_code
                else "Invalid or expired code."
            ),
        }
    session = result.session
    if session is None:
        return {
            "error": "elevation_create_failed",
            "message": "Elevation window not retrievable after create.",
        }
    elevated_until = float(
        session.last_touched_at + elevated_session_manager._idle_timeout
    )
    max_until = float(session.elevated_at + elevated_session_manager._max_age)
    return {
        "elevated": True,
        "scope": result.scope,
        "elevated_until": elevated_until,
        "max_until": max_until,
    }


# ---------------------------------------------------------------------------
# Public handler
# ---------------------------------------------------------------------------


def elevate_session(
    args: Dict[str, Any],
    user: User,
    session_key: str = "",
    http_request: Optional[Request] = None,
) -> Dict[str, Any]:
    """Submit a TOTP or recovery code to open an MCP elevation window (Story #925 AC3).

    ``session_key`` and ``http_request`` are injected by the MCP dispatcher.
    """
    return _mcp_response(_elevate(args, user, session_key, _client_ip(http_request)))


def _elevate(
    args: Dict[str, Any], user: User, session_key: str, client_ip: str
) -> Dict[str, Any]:
    """Run the elevation checks and return the unwrapped result payload."""
    if not _is_elevation_enforcement_enabled():
        return {
            "error": "elevation_enforcement_disabled",
            "message": "Step-up elevation is currently disabled by the operator.",
        }

    arg_error = _validate_elevate_args(args)
    if arg_error is not None:
        return arg_error

    totp_svc = get_totp_service()
    if totp_svc is None:
        return {
            "error": "elevation_enforcement_disabled",
            "message": "TOTP service not available.",
        }
    if not totp_svc.is_mfa_enabled(user.username):
        return {
            "error": "totp_setup_required",
            "setup_url": _mfa_setup_url_for_role(user.role),
            "message": "Set up TOTP before performing this action.",
        }

    # Resolve the session key before any code is verified, so a code is never
    # consumed on a request that cannot hold an elevation window.
    if not session_key:
        return {
            "error": "missing_session_key",
            "message": "No session key on MCP request.",
        }

    # The shared step-up: same limiter and key as REST /auth/elevate, so
    # failed attempts through any front door count against one lockout.
    return _step_up_payload(
        step_up(
            user.username,
            totp_code=args.get("totp_code"),
            recovery_code=args.get("recovery_code"),
            session_key=session_key,
            client_ip=client_ip,
            totp_service=totp_svc,
            sessions=elevated_session_manager,
            limiter=login_rate_limiter,
        )
    )
