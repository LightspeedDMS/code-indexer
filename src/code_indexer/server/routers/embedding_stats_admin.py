"""Admin query endpoint for embedding & reranker call tracking (Story #1418
Phase 3 Component 7, vendor cost reconciliation).

Mirrors the hnsw_orphan_sweep_admin.py pattern (Story #1360): a small
dedicated admin-only REST endpoint reading straight off
``backend_registry.embedding_call_stats`` (Phase 1's dual-backend
EmbeddingCallStatsSqliteBackend / EmbeddingCallStatsPostgresBackend), never
coupled to BackgroundJobManager/JobTracker.
"""

from dataclasses import asdict
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User

router = APIRouter(prefix="/api/admin/embedding-stats", tags=["embedding-stats-admin"])

# Entry-point bounds for the pagination params -- rejects negative offsets
# and out-of-range limits before they ever reach the backend query. Plain
# int defaults (NOT FastAPI's Query(...) declarative validation) are used
# deliberately -- Query(...) objects only resolve to their default value
# via FastAPI's own dependency-injection machinery, which would break this
# endpoint's direct-function-call testability (mirrors
# hnsw_orphan_sweep_admin.py's own plain-int-default convention).
_MAX_QUERY_LIMIT = 1000


@router.get("/query")
def query_embedding_call_stats(
    request: Request,
    provider: Optional[str] = None,
    purpose: Optional[str] = None,
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    limit: int = 200,
    offset: int = 0,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Query embedding_call_stats records, filtered by any combination of
    provider/purpose/golden_repo_alias/job_id/time-range.

    Raises:
        HTTPException: 400 if limit/offset are out of range.
        HTTPException: 503 if the backend registry (or its
            embedding_call_stats field) is not available (e.g. server not
            fully initialized).
    """
    if limit < 1 or limit > _MAX_QUERY_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {_MAX_QUERY_LIMIT}",
        )
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")

    backend_registry = getattr(request.app.state, "backend_registry", None)
    if (
        backend_registry is None
        or getattr(backend_registry, "embedding_call_stats", None) is None
    ):
        raise HTTPException(
            status_code=503,
            detail="Embedding call stats backend is not available "
            "(backend registry not initialized)",
        )

    records = backend_registry.embedding_call_stats.query(
        provider=provider,
        purpose=purpose,
        golden_repo_alias=golden_repo_alias,
        job_id=job_id,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )
    return {
        "records": [asdict(r) for r in records],
        "count": len(records),
    }
