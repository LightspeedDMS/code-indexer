"""Web UI elevation flow (Story #923 AC7).

Provides:
- GET /admin/elevate?next=... -- render elevation form
- POST /auth/elevate-form -- process form submission, redirect to next on success
- POST /auth/elevate-ajax -- AJAX modal elevation, returns JSON (Bug #955)

First-run guidance: when user lacks TOTP, GET redirects to /admin/mfa/setup?next=...
so the admin can complete TOTP setup and bounce back.
"""

import logging
import os
from enum import Enum, auto
from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from code_indexer.server.auth.dependencies import (
    _is_elevation_enforcement_enabled,
    get_current_admin_user_hybrid,
)
from code_indexer.server.auth.elevated_session_manager import elevated_session_manager
from code_indexer.server.auth.user_manager import User
from code_indexer.server.web.mfa_routes import get_totp_service

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

router = APIRouter(tags=["elevation_web"])

_DEFAULT_NEXT = "/admin/"

# Named HTTP status constants — avoids magic numbers throughout this module.
_HTTP_SEE_OTHER = status.HTTP_303_SEE_OTHER
_HTTP_BAD_REQUEST = status.HTTP_400_BAD_REQUEST
_HTTP_UNAUTHORIZED = status.HTTP_401_UNAUTHORIZED
_HTTP_FORBIDDEN = status.HTTP_403_FORBIDDEN
_HTTP_SERVICE_UNAVAILABLE = status.HTTP_503_SERVICE_UNAVAILABLE


class _ElevResult(Enum):
    SUCCESS = auto()
    KILL_SWITCH_OFF = auto()
    NO_CODE = auto()
    NO_MFA = auto()
    NO_SESSION = auto()
    INVALID_CODE = auto()


def _sanitize_next(next_value: str) -> str:
    """Return a safe relative in-app path from caller-supplied next value.

    Only paths that start with a single '/' (not '//' which signals a
    protocol-relative URL and can be used for open-redirect attacks) and
    contain no scheme separator ('://') are accepted.  Everything else
    falls back to _DEFAULT_NEXT.

    Args:
        next_value: Caller-supplied redirect destination.

    Returns:
        A validated relative path string.
    """
    if (
        isinstance(next_value, str)
        and next_value.startswith("/")
        and not next_value.startswith("//")
        and "://" not in next_value
    ):
        return next_value
    logger.warning(
        "Rejecting unsafe next= redirect value %r — falling back to %s",
        next_value,
        _DEFAULT_NEXT,
    )
    return _DEFAULT_NEXT


def _elev_error(request: Request, safe_next: str, message: str, http_status: int):
    """Render elevation form with an error message at the given HTTP status."""
    return templates.TemplateResponse(
        "elevate.html",
        {"request": request, "next": safe_next, "error": message},
        status_code=http_status,
    )


def _redirect_to_setup(safe_next: str):
    """Redirect to MFA setup, bouncing back to safe_next afterwards."""
    return RedirectResponse(
        url=f"/admin/mfa/setup?next={quote(safe_next, safe='')}",
        status_code=_HTTP_SEE_OTHER,
    )


def _resolve_session_key(request: Request) -> Optional[str]:
    """Resolve session_key from JWT jti (Bearer) or cidx_session cookie (Web UI)."""
    jti = getattr(getattr(request, "state", None), "user_jti", None)
    if jti:
        return str(jti)
    cookie = request.cookies.get("cidx_session")
    return str(cookie) if cookie is not None else None


def _verify_credentials(
    totp_service, username: str, totp_code, recovery_code, client_ip: str
):
    """Verify TOTP or recovery code; return scope string or None on failure.

    Args:
        totp_service: Active TOTPService instance.
        username: Authenticated admin username.
        totp_code: TOTP code from form (may be None).
        recovery_code: Recovery code from form (may be None).
        client_ip: Client IP for audit purposes.

    Returns:
        Elevation scope string ("full" or "totp_repair") on success, None on failure.
    """
    if recovery_code:
        if totp_service.verify_recovery_code(
            username, recovery_code, ip_address=client_ip
        ):
            return "totp_repair"
        return None
    if totp_service.verify_enabled_code(username, totp_code):
        return "full"
    return None


def _attempt_elevation(
    request: Request,
    username: str,
    totp_code: Optional[str],
    recovery_code: Optional[str],
    client_ip: str,
) -> Tuple[_ElevResult, Optional[str]]:
    """Run the shared elevation decision pipeline.

    Executes all validation steps (kill-switch, code presence, MFA config,
    session key, credential verification) and — on success — creates the
    elevated session.  All audit log entries are emitted here so both the
    form and AJAX callers share identical observability.

    Args:
        request: Current FastAPI request (used for session key resolution).
        username: Authenticated admin username.
        totp_code: TOTP code supplied by the caller (may be None).
        recovery_code: Recovery code supplied by the caller (may be None).
        client_ip: Client IP address for audit logging.

    Returns:
        A tuple of (_ElevResult, scope_or_None).  scope is only set on
        SUCCESS; all other results carry None as the second element.
    """
    if not _is_elevation_enforcement_enabled():
        logger.warning(
            "Elevation attempt by %s from %s rejected — kill switch is OFF",
            username,
            client_ip,
        )
        return _ElevResult.KILL_SWITCH_OFF, None

    if not totp_code and not recovery_code:
        logger.warning(
            "Elevation attempt by %s from %s rejected — no code provided",
            username,
            client_ip,
        )
        return _ElevResult.NO_CODE, None

    totp_service = get_totp_service()
    if totp_service is None or not totp_service.is_mfa_enabled(username):
        logger.warning(
            "Elevation attempt by %s from %s rejected — no MFA configured",
            username,
            client_ip,
        )
        return _ElevResult.NO_MFA, None

    session_key = _resolve_session_key(request)
    if not session_key:
        logger.warning(
            "Elevation attempt by %s from %s rejected — no session key resolved",
            username,
            client_ip,
        )
        return _ElevResult.NO_SESSION, None

    scope = _verify_credentials(
        totp_service, username, totp_code, recovery_code, client_ip
    )
    if scope is None:
        code_type = "recovery code" if recovery_code else "TOTP code"
        logger.warning(
            "Elevation attempt by %s from %s rejected — invalid %s",
            username,
            client_ip,
            code_type,
        )
        return _ElevResult.INVALID_CODE, None

    elevated_session_manager.create(
        session_key=session_key,
        username=username,
        elevated_from_ip=client_ip,
        scope=scope,
    )
    logger.info(
        "Elevation granted for %s from %s (scope=%s)", username, client_ip, scope
    )
    return _ElevResult.SUCCESS, scope


@router.get("/admin/elevate", response_class=HTMLResponse)
def elevate_page(
    request: Request,
    next: str = _DEFAULT_NEXT,
    user: User = Depends(get_current_admin_user_hybrid),
):
    """Render the elevation form. Redirect to TOTP setup if user has no MFA."""
    safe_next = _sanitize_next(next)
    totp_service = get_totp_service()
    if totp_service is not None and not totp_service.is_mfa_enabled(user.username):
        logger.warning(
            "User %s has no MFA configured — redirecting to setup (next=%s)",
            user.username,
            safe_next,
        )
        return _redirect_to_setup(safe_next)
    return templates.TemplateResponse(
        "elevate.html",
        {"request": request, "next": safe_next, "error": None},
    )


@router.post("/auth/elevate-form")
def elevate_form(
    request: Request,
    next: str = Form(_DEFAULT_NEXT),
    totp_code: Optional[str] = Form(None),
    recovery_code: Optional[str] = Form(None),
    user: User = Depends(get_current_admin_user_hybrid),
):
    """Process Web UI form submission and redirect on success."""
    safe_next = _sanitize_next(next)
    client_ip = request.client.host if request.client else "unknown"

    result, _scope = _attempt_elevation(
        request, user.username, totp_code, recovery_code, client_ip
    )

    if result == _ElevResult.SUCCESS:
        return RedirectResponse(url=safe_next, status_code=_HTTP_SEE_OTHER)
    if result == _ElevResult.KILL_SWITCH_OFF:
        return _elev_error(
            request,
            safe_next,
            "Step-up elevation is currently disabled.",
            _HTTP_SERVICE_UNAVAILABLE,
        )
    if result == _ElevResult.NO_CODE:
        return _elev_error(request, safe_next, "Provide a code.", _HTTP_BAD_REQUEST)
    if result == _ElevResult.NO_MFA:
        return _redirect_to_setup(safe_next)
    if result == _ElevResult.NO_SESSION:
        return _elev_error(request, safe_next, "No session.", _HTTP_FORBIDDEN)
    # INVALID_CODE
    error_msg = "Invalid recovery code." if recovery_code else "Invalid code."
    return _elev_error(request, safe_next, error_msg, _HTTP_UNAUTHORIZED)


@router.post("/auth/elevate-ajax")
def elevate_ajax(
    request: Request,
    totp_code: Optional[str] = Form(None),
    recovery_code: Optional[str] = Form(None),
    user: User = Depends(get_current_admin_user_hybrid),
):
    """AJAX endpoint for inline modal elevation — returns JSON, never redirects."""
    client_ip = request.client.host if request.client else "unknown"

    result, _scope = _attempt_elevation(
        request, user.username, totp_code, recovery_code, client_ip
    )

    if result == _ElevResult.SUCCESS:
        return JSONResponse({"success": True})
    if result == _ElevResult.KILL_SWITCH_OFF:
        return JSONResponse(
            {"success": False, "error": "Step-up elevation is currently disabled."},
            status_code=_HTTP_SERVICE_UNAVAILABLE,
        )
    if result == _ElevResult.NO_CODE:
        return JSONResponse(
            {"success": False, "error": "Provide a code."},
            status_code=_HTTP_BAD_REQUEST,
        )
    if result == _ElevResult.NO_MFA:
        return JSONResponse(
            {"success": False, "error": "No MFA configured. Please set up TOTP first."},
            status_code=_HTTP_BAD_REQUEST,
        )
    if result == _ElevResult.NO_SESSION:
        return JSONResponse(
            {"success": False, "error": "No session."},
            status_code=_HTTP_FORBIDDEN,
        )
    # INVALID_CODE
    error_msg = "Invalid recovery code." if recovery_code else "Invalid code."
    return JSONResponse(
        {"success": False, "error": error_msg},
        status_code=_HTTP_UNAUTHORIZED,
    )
