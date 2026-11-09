"""Admin REST routes of SCIP's self-healing workspace cleanup (Story #647).

- ``POST /api/admin/scip-cleanup-workspaces`` -- run the cleanup now (AC4);
- ``GET /api/admin/scip-cleanup-status`` -- the cleanup status (AC5).

Contributed through the feature's ``routers()`` (modular phase 4d; they were
core inline admin routes): the core authenticates the caller and requires
:data:`ROUTE_PERMISSION`, the administrators' permission, before a handler
runs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status

from cidx_scip.server import runtime
from cidx_scip.server._core import format_error_log

logger = logging.getLogger(__name__)

#: The permission the core checks before any route here runs (admins only).
ROUTE_PERMISSION = "manage_users"

router = APIRouter(tags=["scip-admin"])


def _cleanup_service() -> Any:
    service = runtime.WORKSPACE_CLEANUP.get()
    if service is None:
        raise RuntimeError("the SCIP workspace cleanup service is not started")
    return service


@router.post("/api/admin/scip-cleanup-workspaces")
def scip_cleanup_workspaces() -> Dict[str, Any]:
    """Manually trigger SCIP workspace cleanup (Story #647 - AC4).

    Returns the cleanup summary: deleted_count, preserved_count,
    space_freed_mb, errors.
    """
    try:
        result = _cleanup_service().cleanup_workspaces()
        return {
            "deleted_count": result.workspaces_deleted,
            "preserved_count": result.workspaces_preserved,
            "space_freed_mb": round(result.space_reclaimed_bytes / (1024 * 1024), 2),
            "errors": result.errors,
        }
    except Exception as e:
        logger.error(
            format_error_log("APP-GENERAL-030", f"SCIP workspace cleanup failed: {e}"),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Workspace cleanup failed: {str(e)}",
        )


@router.get("/api/admin/scip-cleanup-status")
def get_scip_cleanup_status() -> Dict[str, Any]:
    """SCIP workspace cleanup status (Story #647 - AC5): last cleanup time,
    workspace count, oldest workspace age, total size."""
    try:
        status_info: Dict[str, Any] = _cleanup_service().get_cleanup_status()
        return status_info
    except Exception as e:
        logger.error(
            format_error_log(
                "APP-GENERAL-031", f"Failed to get SCIP cleanup status: {e}"
            ),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get cleanup status: {str(e)}",
        )
