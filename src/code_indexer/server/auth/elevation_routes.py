"""Elevation endpoints for TOTP step-up authentication (Story #923 AC3+AC4)."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from code_indexer.server.auth.dependencies import (
    get_current_admin_user_hybrid,
    _is_elevation_enforcement_enabled,
)
from code_indexer.server.auth.elevated_session_manager import (
    ElevatedSession,
    elevated_session_manager,
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


def _verify_elevation_code(
    totp_service, username: str, body: ElevateRequest, client_ip: str
) -> str:
    """Verify TOTP or recovery code. Returns scope string on success, raises 401/403 on failure."""
    if body.recovery_code:
        if not totp_service.verify_recovery_code(
            username, body.recovery_code, ip_address=client_ip
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "elevation_failed",
                    "message": "Invalid recovery code.",
                },
            )
        return "totp_repair"
    if not totp_service.verify_enabled_code(username, body.totp_code):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "elevation_failed", "message": "Invalid or expired code."},
        )
    return "full"


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post("/elevate")
def elevate(
    body: ElevateRequest,
    request: Request,
    user: User = Depends(get_current_admin_user_hybrid),
):
    """Submit a TOTP or recovery code to open an elevation window (AC3)."""
    if not _is_elevation_enforcement_enabled():
        raise _kill_switch_exc()
    _validate_elevate_request(body)

    totp_service = _require_totp_service()
    if not totp_service.is_mfa_enabled(user.username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "totp_setup_required", "setup_url": "/admin/mfa/setup"},
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
    limiter_key = f"{client_ip}:{user.username}"

    is_locked, _ = login_rate_limiter.is_locked(limiter_key)
    if is_locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "message": "Too many elevation attempts. Try again later.",
            },
        )

    try:
        scope = _verify_elevation_code(totp_service, user.username, body, client_ip)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            login_rate_limiter.check_and_record_failure(limiter_key)
        raise

    elevated_session_manager.create(
        session_key=session_key,
        username=user.username,
        elevated_from_ip=client_ip,
        scope=scope,
    )
    session = elevated_session_manager.get_status(session_key)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "elevation_create_failed",
                "message": "Elevation window not retrievable after create.",
            },
        )

    login_rate_limiter.record_success(limiter_key)
    resp = _build_status_response(session)
    assert resp.elevated_until is not None
    assert resp.max_until is not None
    return ElevateResponse(
        elevated=True,
        elevated_until=resp.elevated_until,
        max_until=resp.max_until,
        scope=resp.scope or scope,
    )


@router.get("/elevation-status", response_model=StatusResponse)
def elevation_status(
    request: Request,
    user: User = Depends(get_current_admin_user_hybrid),
):
    """Read-only elevation window check — does NOT touch (AC4)."""
    if not _is_elevation_enforcement_enabled():
        return _not_elevated()
    session_key = _resolve_session_key(request)
    if not session_key:
        return _not_elevated()
    session = elevated_session_manager.get_status(session_key)
    if session is None:
        return _not_elevated()
    return _build_status_response(session)
