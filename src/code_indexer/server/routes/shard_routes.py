"""C6 repo-sharding observability endpoint.

Exposes this pod's shard-routing view so sharding can be inspected without
exec-ing into pods: node identity, the live node ring, peer addresses,
replication factor, and forward counters. Reports "disabled" in solo/sqlite
mode (no shard router wired).
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

from ..auth.dependencies import get_current_user
from ..auth.user_manager import User

router = APIRouter(prefix="/api/shard", tags=["shard"])


@router.get("/status")
def shard_status(
    raw_request: Request,
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return this pod's C6 shard-routing status.

    In cluster mode: ``{sharding: "enabled", node_id, replicas, active_nodes,
    node_addresses, forwards, forward_failures}``. In solo/sqlite mode:
    ``{sharding: "disabled"}``.
    """
    shard_router = getattr(raw_request.app.state, "shard_router", None)
    if shard_router is None:
        return {"sharding": "disabled"}
    stats: Dict[str, Any] = shard_router.stats()
    stats["sharding"] = "enabled"
    return stats
