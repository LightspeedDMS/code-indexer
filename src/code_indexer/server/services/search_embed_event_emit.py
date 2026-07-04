"""emit_embed_event -- the single shared emit helper (Story #1293, Epic #1288).

Algorithm 3: driven ENTIRELY by the returned enriched EmbeddingCacheMetadata.
Deduplicates the 4 meta->ctx sites (filesystem_vector_store.py,
search_service.py, temporal_search_service.py, mcp/handlers/search.py) into
ONE emission chokepoint.

No-op (documented, not a silent failure) when:
  - meta.role or meta.outcome is None -- the decision has not been classified
    yet. This is the case for the Path A coalescer.submit() construction
    sites until Story #1293 S1b wires the owner/joiner distinction.
  - no SearchEmbedEventWriter is installed (CLI / solo / pre-lifespan).

correlation_id is NEVER null: reads get_current_correlation_id() (the WIRED
telemetry/correlation_bridge reader -- Story #1293's MCP wrong-import fix)
and falls back to a fresh UUID when that returns None, so genuinely
parentless background contexts still get a durable, non-null id.
"""

import logging
import socket
import time
import uuid
from typing import Any, Optional

from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
from code_indexer.server.services.search_embed_event_writer import (
    SearchEmbedEventRecord,
)
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-level writer accessor (mirrors governed_call.py's
# get/set/clear_query_embedding_cache pattern). None on CLI / pre-lifespan --
# emit_embed_event treats that as a documented no-op branch.
# ---------------------------------------------------------------------------

_search_embed_event_writer: Any = None


def get_search_embed_event_writer() -> Any:
    """Return the process-level SearchEmbedEventWriter, or None (CLI/solo)."""
    return _search_embed_event_writer


def set_search_embed_event_writer(writer: Any) -> None:
    """Install the process-level writer (called once in lifespan startup)."""
    global _search_embed_event_writer
    _search_embed_event_writer = writer


def clear_search_embed_event_writer() -> None:
    """Clear the process-level writer (lifespan shutdown / test isolation)."""
    global _search_embed_event_writer
    _search_embed_event_writer = None


# ---------------------------------------------------------------------------
# node_id resolution -- mirrors the stepwise lookup already duplicated at
# mcp/handlers/search.py's SearchEventRecord construction site (Story #1159).
# ---------------------------------------------------------------------------


def _resolve_node_id() -> str:
    """Stepwise node_id lookup -- never raises (getattr with defaults)."""
    try:
        from code_indexer.server.services.config_service import get_config_service

        cfg_svc = get_config_service()
        cfg_get = getattr(cfg_svc, "get_config", None)
        cfg_obj = cfg_get() if callable(cfg_get) else None
        node_id = str(getattr(cfg_obj, "node_id", "") or "")
    except Exception as exc:  # noqa: BLE001 -- node_id must never break emission
        logger.debug("emit_embed_event: config node_id lookup failed: %s", exc)
        node_id = ""

    if not node_id:
        try:
            node_id = socket.gethostname()
        except OSError as exc:
            logger.debug("emit_embed_event: gethostname() failed: %s", exc)
            node_id = "unknown"
    return node_id


# ---------------------------------------------------------------------------
# The shared emit helper
# ---------------------------------------------------------------------------


def emit_embed_event(
    meta: EmbeddingCacheMetadata,
    *,
    correlation_id: Optional[str] = None,
) -> None:
    """Persist one query-embedding decision event, driven entirely by meta.

    Args:
        meta: The EmbeddingCacheMetadata returned by coalesced_query_embedding
            (or coalescer.submit()). Must have outcome AND role populated --
            both None (the Path A / not-yet-classified case) is a documented
            no-op.
        correlation_id: Optional override. When None (the normal case), the
            current request-scoped correlation id is read via
            get_current_correlation_id() with a UUID fallback so no event is
            EVER written with a null correlation_id.
    """
    if meta.role is None or meta.outcome is None:
        logger.debug(
            "emit_embed_event: meta not yet classified (role=%r outcome=%r) "
            "-- skipping emission (Path A coalescer wiring lands in Story "
            "#1293 S1b)",
            meta.role,
            meta.outcome,
        )
        return

    writer = get_search_embed_event_writer()
    if writer is None:
        return  # CLI / solo / pre-lifespan -- documented no-op branch

    resolved_correlation_id = correlation_id or get_current_correlation_id()
    if not resolved_correlation_id:
        resolved_correlation_id = str(uuid.uuid4())

    record = SearchEmbedEventRecord(
        timestamp=time.time(),
        correlation_id=resolved_correlation_id,
        node_id=_resolve_node_id(),
        provider=meta.provider or "",
        model=meta.model,
        config_digest=meta.config_digest,
        cache_mode=meta.cache_mode,
        outcome=meta.outcome,
        role=meta.role,
        live_batch_id=meta.live_batch_id,
        embed_key=meta.embed_key,
        long_key=meta.long_key,
        latency_ms=meta.provider_latency_ms,
        shadow_cosine=meta.shadow_cosine,
    )
    writer.enqueue(record)
