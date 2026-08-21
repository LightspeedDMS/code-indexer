"""
Admin REST endpoint for bulk-clearing fleet-migration dedup-state warnings
from the Diagnostics tab (Story #1589).

Fleet-migration dedup repair (Story #1560) records a per-repo outcome row
whenever it auto-resolves duplicate point-ids during consolidation and
permanently drops records. Those rows surface as `/health` DEGRADED
`failure_reasons` entries (health_service.py's
`_collect_fleet_migration_dedup_failures`) until cleared. This endpoint
lets an operator dismiss ALL currently-active warnings in one action --
e.g. after manually reviewing them and confirming the data-completeness
loss is acceptable -- without being forced to run a full re-index of
every affected repo just to silence the Degraded status.

Mirrors admin_provider_health.py's /reset-state endpoint: a state-clearing
admin action gated by both `get_current_admin_user_hybrid` (403 for
non-admin) and `require_elevation()` (TOTP step-up when elevation
enforcement is administratively enabled; the kill switch inside
require_elevation makes this a plain admin-only check whenever enforcement
is off, which is this project's default -- see auth/dependencies.py).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import dependencies
from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User
from ..services.fleet_migration.dedup_state import (
    DedupStateUnavailableError,
    clear_all_dedup_states,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/diagnostics", tags=["dedup-warnings-admin"])

# Story #1589 AC1: the exact reason string every cleared row must carry,
# per the acceptance criteria's Gherkin.
CLEAR_ALL_DEDUP_REASON = "manually acknowledged via Diagnostics tab"


class ClearAllDedupWarningsResponse(BaseModel):
    """Response for POST /api/admin/diagnostics/dedup-warnings/clear-all."""

    cleared_count: int


@router.post(
    "/dedup-warnings/clear-all",
    response_model=ClearAllDedupWarningsResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def clear_all_dedup_warnings(
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> ClearAllDedupWarningsResponse:
    """Bulk-clear every currently-active fleet-migration dedup-state
    warning (Story #1589). Idempotent: a repeat call reports
    cleared_count=0 once nothing remains active.

    Raises:
        HTTPException: 503 if golden_repo_manager is not initialized;
            500 if the underlying storage backend genuinely fails.
    """
    golden_repo_manager = getattr(request.app.state, "golden_repo_manager", None)
    if golden_repo_manager is None:
        raise HTTPException(
            status_code=503,
            detail="golden_repo_manager not initialized",
        )

    try:
        cleared_count = clear_all_dedup_states(
            golden_repo_manager, reason=CLEAR_ALL_DEDUP_REASON
        )
    except DedupStateUnavailableError as exc:
        logger.error("Story #1589: clear-all dedup warnings failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to clear dedup warnings: {exc}",
        )

    logger.info(
        "Story #1589: admin %s cleared %d active fleet-migration dedup "
        "warning(s) via the Diagnostics tab",
        getattr(current_user, "username", "<unknown>"),
        cleared_count,
    )
    return ClearAllDedupWarningsResponse(cleared_count=cleared_count)
