"""Elevation endpoints for TOTP step-up authentication (Story #923 AC3+AC4)."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from code_indexer.server.auth.dependencies import (
    get_current_user_hybrid,
    _is_elevation_enforcement_enabled,
    _mfa_setup_url_for_role,
)
from code_indexer.server.auth.elevated_session_manager import (
    ElevatedSession,
    elevated_session_manager,
)
from code_indexer.server.auth.elevation_step_up import (
    StepUpOutcome,
    StepUpResult,
    step_up,
)
from code_indexer.server.auth.login_rate_limiter import login_rate_limiter
from code_indexer.server.auth.user_manager import User
from code_indexer.server.web.mfa_routes import get_totp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["elevation"])


class ElevateRequest(BaseModel):
    totp_code: Optional[str] = None
    recovery_code: Optional[str] = None


class ElevateResponse(BaseModel):
    elevated: bool
    elevated_until: float
    max_until: float
    scope: str


class StatusResponse(BaseModel):
    elevated: bool
    elevated_until: Optional[float] = None
    max_until: Optional[float] = None
    scope: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_session_key(request: Request) -> Optional[str]:
    """Resolve session_key from JWT jti (Bearer) or cidx_session cookie (Web UI)."""
    jti = getattr(getattr(request, "state", None), "user_jti", None)
    if jti:
        return str(jti)
    cookie = request.cookies.get("cidx_session")
    return str(cookie) if cookie is not None else None


def _kill_switch_exc() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "elevation_enforcement_disabled",
            "message": "Step-up elevation is currently disabled by the operator.",
        },
    )


def _build_status_response(session: ElevatedSession) -> StatusResponse:
    """Build a StatusResponse from an active ElevatedSession."""
    return StatusResponse(
        elevated=True,
        elevated_until=session.last_touched_at + elevated_session_manager._idle_timeout,
        max_until=session.elevated_at + elevated_session_manager._max_age,
        scope=getattr(session, "scope", "full") or "full",
    )


def _not_elevated() -> StatusResponse:
    return StatusResponse(elevated=False)


def _validate_elevate_request(body: ElevateRequest) -> None:
    """Raise 400 if request body is malformed (missing or ambiguous code)."""
    if not body.totp_code and not body.recovery_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_code",
                "message": "Provide totp_code or recovery_code.",
            },
        )
    if body.totp_code and body.recovery_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "ambiguous_code",
                "message": "Provide totp_code OR recovery_code, not both.",
            },
        )


def _require_totp_service():
    """Return the live TOTPService or raise 503 if unavailable."""
    svc = get_totp_service()
    if svc is None:
        raise _kill_switch_exc()
    return svc


def _step_up_error(result: StepUpResult) -> HTTPException:
    """This door's error for a step-up that did not grant a window."""
    if result.outcome is StepUpOutcome.BUSY:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "busy",
                "message": "Elevation is busy, try again shortly.",
            },
            headers=result.retry_after_header(),
        )
    if result.outcome is StepUpOutcome.LOCKED_OUT:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "message": "Too many elevation attempts. Try again later.",
            },
            headers=result.retry_after_header(),
        )
    if result.outcome is StepUpOutcome.INVALID_CODE:
        message = (
            "Invalid recovery code."
            if result.used_recovery_code
            else "Invalid or expired code."
        )
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "elevation_failed", "message": message},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": "elevation_create_failed",
            "message": "Elevation window not retrievable after create.",
        },
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post("/elevate")
def elevate(
    body: ElevateRequest,
    request: Request,
    user: User = Depends(get_current_user_hybrid),
):
    """Submit a TOTP or recovery code to open an elevation window (AC3).

    Elevation is available to every TOTP-enrolled user, not only admins --
    this opens a window for the CALLER's own username; it never grants any
    admin-only action, which stays behind require_elevation()'s own admin
    gate on each protected route.

    When the kill switch is OFF, this endpoint has no meaning — the caller is
    asking to satisfy a TOTP challenge that no protected route will issue.
    Return 503 (`elevation_enforcement_disabled`) so divergent callers fail
    loudly instead of being silently passed through (anti-fallback / Rule 2 +
    anti-silent-failure / Rule 13).
    """
    if not _is_elevation_enforcement_enabled():
        raise _kill_switch_exc()
    _validate_elevate_request(body)

    totp_service = _require_totp_service()
    if not totp_service.is_mfa_enabled(user.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "totp_setup_required",
                "setup_url": _mfa_setup_url_for_role(user.role),
            },
        )

    session_key = _resolve_session_key(request)
    if not session_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "elevation_required",
                "message": "No session key on request.",
            },
        )

    client_ip = request.client.host if request.client else "unknown"
    result = step_up(
        user.username,
        totp_code=body.totp_code,
        recovery_code=body.recovery_code,
        session_key=session_key,
        client_ip=client_ip,
        totp_service=totp_service,
        sessions=elevated_session_manager,
        limiter=login_rate_limiter,
    )
    if result.session is None:
        raise _step_up_error(result)

    resp = _build_status_response(result.session)
    assert resp.elevated_until is not None
    assert resp.max_until is not None
    return ElevateResponse(
        elevated=True,
        elevated_until=resp.elevated_until,
        max_until=resp.max_until,
        scope=resp.scope or result.scope,
    )


@router.get("/elevation-status", response_model=StatusResponse)
def elevation_status(
    request: Request,
    user: User = Depends(get_current_user_hybrid),
):
    """Read-only elevation window check — does NOT touch (AC4).

    An elevation window is valid only for the user who created it: a window
    resolved for this session key but owned by a different user is treated
    exactly like "no window" -- never disclosed as this user's own status.
    """
    if not _is_elevation_enforcement_enabled():
        return _not_elevated()
    session_key = _resolve_session_key(request)
    if not session_key:
        return _not_elevated()
    session = elevated_session_manager.get_status(session_key)
    if session is None:
        return _not_elevated()
    if session.username != user.username:
        logger.warning(
            "Elevation status lookup rejected: session key %.8s is not "
            "owned by the authenticating user %s",
            session_key,
            user.username,
        )
        return _not_elevated()
    return _build_status_response(session)
