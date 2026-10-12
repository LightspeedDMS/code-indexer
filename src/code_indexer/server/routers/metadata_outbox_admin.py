"""
Admin REST endpoint to abandon the control-plane metadata outbox (content
seam step 5a).

During a rollback the leader drains the outbox to the alias / write-mode
files, bounded by ``metadata_outbox_drain_timeout_s``. If keys will not drain,
an operator overrides: this endpoint logs every remaining key by name, deletes
exactly those rows and stops the sweeper -- depth is then 0 and the rollback
proceeds. With ``metadata_read_source=files`` those keys are served from the
files as they are: stale by exactly the logged keys, which the operator
re-applies by hand.

A destructive admin action, so it is gated like the other state-clearing
admin endpoints: ``get_current_admin_user_hybrid`` (403 for a non-admin) and
``require_elevation()`` (403 ``elevation_required`` without an active TOTP
elevation window when elevation enforcement is on).

Cluster-safe from any node: the rows and the stop flag are in the shared
registry, so an abandon handled by a follower stops the leader's sweeper.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import dependencies
from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/metadata-outbox", tags=["metadata-outbox-admin"])


class AbandonedKey(BaseModel):
    kind: str
    name: str


class AbandonOutboxResponse(BaseModel):
    """Response for POST /api/admin/metadata-outbox/abandon."""

    abandoned: List[AbandonedKey]
    count: int


@router.post(
    "/abandon",
    response_model=AbandonOutboxResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def abandon_metadata_outbox(
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> AbandonOutboxResponse:
    """Log, delete exactly the undrained outbox rows, and stop the sweeper.

    Raises:
        HTTPException: 503 when no outbox sweeper is running on this server.
    """
    sweeper = getattr(request.app.state, "metadata_outbox_sweeper", None)
    if sweeper is None:
        raise HTTPException(
            status_code=503, detail="metadata outbox sweeper not initialized"
        )
    keys = sweeper.abandon(requested_by=current_user.username)
    return AbandonOutboxResponse(
        abandoned=[AbandonedKey(kind=kind, name=name) for kind, name in keys],
        count=len(keys),
    )
