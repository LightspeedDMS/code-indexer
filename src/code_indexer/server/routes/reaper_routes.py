"""
Activated Repository Reaper routes (Story #967).

Provides a dedicated REST endpoint for manually triggering a reaper cycle:
  POST /api/admin/reaper/trigger
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole


router = APIRouter()


def _require_admin(user: User = Depends(get_current_user)) -> User:
    """
    Dependency to require admin role.

    Args:
        user: Current authenticated user

    Returns:
        User if admin

    Raises:
        HTTPException: 403 if user is not admin
    """
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


@router.post("/reaper/trigger")
def trigger_reaper(
    request: Request,
    user: User = Depends(_require_admin),
) -> JSONResponse:
    """
    Manually trigger an activated-repository reaper cycle (Story #967, AC3).

    Admin-only endpoint that submits a 'reap_activated_repos' background job
    immediately, without waiting for the configured cadence.  Used by E2E
    tests and operators who want on-demand housekeeping.

    Args:
        request: FastAPI request (provides app.state.activated_reaper_scheduler).
        user:    Authenticated admin user.

    Returns:
        JSON with job_id and status='submitted'.

    Raises:
        HTTPException 503: If the reaper scheduler is not running.
    """
    scheduler = getattr(request.app.state, "activated_reaper_scheduler", None)
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="Activated reaper scheduler is not running",
        )
    job_id: str = scheduler.trigger_now()
    return JSONResponse({"job_id": job_id, "status": "submitted"})
