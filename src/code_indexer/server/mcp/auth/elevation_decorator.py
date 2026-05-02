"""MCP elevation enforcement decorator (Story #925 AC1).

Codex H6: NOT a nullable helper that skips when no manager wired.
That pattern is silently bypassable. This decorator fails closed: every
code path that cannot confirm an active elevation window returns a
structured MCP error dict instead of calling the handler.

Error codes (mirror REST layer in dependencies.py):
  elevation_enforcement_disabled  - kill switch off or manager/TOTP unavailable
  totp_setup_required             - admin has no TOTP configured (with setup_url)
  elevation_required              - no active window or scope insufficient
"""

import logging
from functools import wraps
from typing import Any, Callable, Dict, Optional

from code_indexer.server.auth.elevated_session_manager import (
    elevated_session_manager,
)
from code_indexer.server.auth.user_manager import User
from code_indexer.server.web.mfa_routes import get_totp_service

logger = logging.getLogger(__name__)

# Stable internal path for TOTP setup — matches REST layer constant.
_TOTP_SETUP_URL = "/admin/mfa/setup"

# Scope hierarchy: rank 0 = broadest ("full"), rank 1 = narrower ("totp_repair").
# A session satisfies required_scope R when session_rank <= required_rank.
_SCOPE_RANK: Dict[str, int] = {"full": 0, "totp_repair": 1}


def _is_elevation_enforcement_enabled() -> bool:
    """Read kill switch from runtime config. Returns False (fails closed) on error."""
    try:
        from code_indexer.server.services.config_service import get_config_service

        config = get_config_service().get_config()
        return bool(getattr(config, "elevation_enforcement_enabled", False))
    except Exception:
        logger.warning(
            "require_mcp_elevation: could not read config; treating as disabled",
            exc_info=True,
        )
        return False


def _disabled_error(
    message: str = "Step-up elevation is currently disabled by the operator.",
) -> Dict[str, Any]:
    return {"error": "elevation_enforcement_disabled", "message": message}


def _elevation_required_error(message: str) -> Dict[str, Any]:
    return {"error": "elevation_required", "message": message}


def _totp_setup_required_error() -> Dict[str, Any]:
    return {
        "error": "totp_setup_required",
        "setup_url": _TOTP_SETUP_URL,
        "message": "Set up TOTP at the URL above before performing this action.",
    }


def require_mcp_elevation(required_scope: str = "full") -> Callable:
    """Decorator factory enforcing TOTP elevation for sensitive MCP tools.

    Usage:
        @require_mcp_elevation()
        def handle_create_user(args, user, session_key=None):
            ...

    Handler signature: (args: Dict, user: User, *extra, **kwargs)
    session_key is resolved from kwargs["session_key"] or extra[0].

    Returns an MCP-style error dict (does NOT raise HTTPException) when
    elevation is missing. Three error codes mirror the REST layer:
      totp_setup_required / elevation_required / elevation_enforcement_disabled

    Kill switch: returns elevation_enforcement_disabled (NOT a silent bypass).
    Manager None: returns elevation_enforcement_disabled (fail closed).
    """
    if required_scope not in _SCOPE_RANK:
        raise ValueError(
            f"required_scope must be one of {sorted(_SCOPE_RANK)}, got {required_scope!r}"
        )

    def decorator(
        handler: Callable[..., Dict[str, Any]],
    ) -> Callable[..., Dict[str, Any]]:
        @wraps(handler)
        def wrapper(
            args: Dict[str, Any], user: User, *extra: Any, **kwargs: Any
        ) -> Dict[str, Any]:
            # Pop session_key early — must not leak to handlers that don't declare it,
            # regardless of which gate fires first.
            session_key: Optional[str] = kwargs.pop("session_key", None) or (
                extra[0] if extra else None
            )

            # Gate 1: kill switch — passthrough when enforcement is disabled.
            # Per operator policy: if elevation is globally off, the handler
            # must proceed without a TOTP challenge.
            if not _is_elevation_enforcement_enabled():
                return handler(args, user, *extra, **kwargs)

            # Gate 2: manager reference (singleton — always non-None after startup)
            esm = elevated_session_manager
            if esm is None:
                return _disabled_error("ElevatedSessionManager not initialised.")

            # Gate 3: TOTP service availability
            totp = get_totp_service()
            if totp is None:
                return _disabled_error("TOTP service not available.")

            # Gate 4: TOTP setup check
            if not totp.is_mfa_enabled(user.username):
                return _totp_setup_required_error()

            # Gate 5: session key resolved above.
            if not session_key:
                return _elevation_required_error("No session key on MCP request.")

            # Gate 6: atomic touch — validates window exists, owned by this user,
            # and resets idle timer. Username binding closes the cross-user bypass.
            session = esm.touch_atomic_for_user(str(session_key), user.username)
            if session is None:
                return _elevation_required_error(
                    "No active elevation window. Call elevate_session first."
                )

            # Gate 7: scope check
            session_scope = getattr(session, "scope", None) or "full"
            session_rank = _SCOPE_RANK.get(session_scope, len(_SCOPE_RANK))
            required_rank = _SCOPE_RANK[required_scope]
            if session_rank > required_rank:
                return _elevation_required_error(
                    f"Full elevation required; current scope={session_scope!r}."
                    if required_scope == "full"
                    else f"Scope {required_scope!r} required; current scope={session_scope!r}."
                )

            # All gates passed — invoke the real handler
            return handler(args, user, *extra, **kwargs)

        wrapper.__mcp_requires_session_key__ = True
        return wrapper

    return decorator
