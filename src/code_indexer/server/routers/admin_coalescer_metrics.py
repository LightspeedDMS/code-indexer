"""REST endpoint for coalescer dedup metrics (Story #1146).

Exposes per-node in-memory counters from the EmbeddingCoalescer instances held
in the process-level CoalescerRegistry.  These are volatile tallies — not
persisted to DB — so the response reflects the current node's observed traffic.

Endpoint: GET /api/admin/coalescer-metrics
Auth: admin (same as other /api/admin/* endpoints)

Counter semantics (mirroring EmbeddingCoalescer docstring):
  texts_coalesced      — requestors admitted to live coalesced batches (unchanged).
  batches_dispatched   — provider HTTP embed calls (one per sealed batch).
  dedup_savings        — sum of (requestors_in_live_batch - unique_provider_texts)
                         across dispatched batches (Story #1146). Excludes cache hits.
  provider_embed_calls — identical to batches_dispatched in meaning; increments by
                         1 per dispatched batch.

Returns zeros when the registry is absent (CLI/solo/pre-lifespan).
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User

router = APIRouter(
    prefix="/api/admin/coalescer-metrics", tags=["admin-coalescer-metrics"]
)


@router.get("")
def get_coalescer_metrics(
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Return aggregated coalescer dedup counters for this node (Story #1146).

    Reads from the process-level CoalescerRegistry. Returns zeros when no
    registry is present (CLI / solo / pre-lifespan nodes).

    Response fields:
      texts_coalesced      — total requestors admitted to live coalesced batches.
      batches_dispatched   — total dispatched batches (== provider HTTP embed calls).
      dedup_savings        — requestors saved from re-embedding by dedup (Story #1146).
      provider_embed_calls — provider HTTP embed call count (1 per batch).
      node_id              — node identifier for cluster E2E attribution.
    """
    from code_indexer.server.services.coalescer_registry import get_coalescer_registry

    registry = get_coalescer_registry()
    if registry is not None:
        metrics = registry.metrics()
    else:
        metrics = {
            "texts_coalesced": 0,
            "batches_dispatched": 0,
            "dedup_savings": 0,
            "provider_embed_calls": 0,
        }

    import socket

    node_id = socket.gethostname()

    return {
        "texts_coalesced": metrics.get("texts_coalesced", 0),
        "batches_dispatched": metrics.get("batches_dispatched", 0),
        "dedup_savings": metrics.get("dedup_savings", 0),
        "provider_embed_calls": metrics.get("provider_embed_calls", 0),
        "node_id": node_id,
    }
