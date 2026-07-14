"""Admin stats endpoint for the HNSW orphan repair fleet sweep
(Story #1360, Epic #1333 S3).

Dashboard pattern (settled 2026-07-11): accumulated cross-pass fleet stats
(last full pass time, total orphans repaired to date, current cursor
position) are NOT modeled as a BackgroundJobManager/JobTracker job -- only
one short tick job per tick is (see scheduler.py). These stats live here,
backed by the SAME durable state_backend the scheduler's cursor uses, read
independently of JobTracker so they are visible even between ticks or on a
node that is not currently running the scheduler.
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User

router = APIRouter(
    prefix="/api/admin/hnsw-orphan-sweep", tags=["hnsw-orphan-sweep-admin"]
)


@router.get("/stats")
def get_hnsw_orphan_sweep_stats(
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Return the durable cross-pass HNSW orphan repair sweep fleet stats.

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
    }
