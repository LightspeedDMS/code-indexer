"""Admin stats endpoint for the HNSW orphan repair fleet sweep
(Story #1360, Epic #1333 S3).

Dashboard pattern (settled 2026-07-11): accumulated cross-pass fleet stats
(last full pass time, total orphans repaired to date, current cursor
position) are NOT modeled as a BackgroundJobManager/JobTracker job -- only
one short tick job per tick is (see scheduler.py). These stats live here,
backed by the SAME durable state_backend the scheduler's cursor uses, read
independently of JobTracker so they are visible even between ticks or on a
node that is not currently running the scheduler.

Bug #1864: that independence is right for the cross-pass counters and WRONG
as the endpoint's entire answer. Reading only the durable row made these
three render byte-identically -- scheduler alive with a pass in progress,
scheduler alive but wedged, and scheduler never started -- which is why a
production node whose ``last_full_pass_completed_at`` had been frozen for two
months raised no signal anywhere. The durable counters are unchanged; a
strictly additive ``local_scheduler`` block now answers the separate question
"is the thing that advances them alive HERE, and when did it last do
anything".

That block is per-PROCESS RAM (the scheduler runs in every worker of every
node) and must never be read as a fleet-wide answer -- CLAUDE.md's
Cluster-Aware State rule. Keeping it nested under its own key, with an
explicit ``scope`` marker, is what enforces that: fleet facts stay at the top
level where they always were, local facts stay inside. It is O(1) with no
filesystem or database access, so it cannot slow the endpoint down at fleet
scale (CLAUDE.md design-for-900-repos).
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User

router = APIRouter(
    prefix="/api/admin/hnsw-orphan-sweep", tags=["hnsw-orphan-sweep-admin"]
)


def _resolve_local_scheduler_liveness(request: Request) -> Dict[str, Any]:
    """Build the per-process liveness block (Bug #1864).

    Both ``app.state`` reads use ``getattr(..., None)`` so a process that
    never reached lifespan's sweep-startup block at all still gets a
    well-formed, honest answer instead of a 500.

    ``startup_error`` is what makes a failed boot visible AFTER the boot log
    has scrolled away: lifespan records the reason its ``except Exception``
    caught (e.g. "backend_registry is not available") and this surfaces it on
    every request. ``observed_at`` is the server's own clock, so a monitor can
    judge ``last_tick_at`` staleness without trusting its own.
    """
    # Imported inside the function so merely importing this router does not
    # pull the sweep service chain (discovery -> repair_executor -> HNSW
    # index machinery) into module-import time.
    from ..services.hnsw_orphan_sweep.scheduler import (
        absent_local_scheduler_liveness,
    )

    scheduler = getattr(request.app.state, "hnsw_orphan_repair_sweep_scheduler", None)
    liveness = (
        absent_local_scheduler_liveness()
        if scheduler is None
        else scheduler.get_liveness()
    )
    liveness["startup_error"] = getattr(
        request.app.state, "hnsw_orphan_repair_sweep_startup_error", None
    )
    liveness["observed_at"] = datetime.now(timezone.utc).isoformat()
    return liveness


@router.get("/stats")
def get_hnsw_orphan_sweep_stats(
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Return the durable cross-pass HNSW orphan repair sweep fleet stats,
    plus this process's scheduler liveness under ``local_scheduler``.

    Raises:
        HTTPException: 503 if the backend registry is not available (e.g.
            server not fully initialized).
    """
    backend_registry = getattr(request.app.state, "backend_registry", None)
    if backend_registry is None:
        raise HTTPException(
            status_code=503,
            detail="HNSW orphan sweep state is not available (backend registry not initialized)",
        )

    state = backend_registry.hnsw_orphan_sweep_state.get_state()
    return {
        **state,
        "current_cursor": state.get("last_completed_key"),
        "local_scheduler": _resolve_local_scheduler_liveness(request),
    }
