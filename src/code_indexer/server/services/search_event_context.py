"""SearchEventContext — per-request context for search event telemetry (Issue #1159).

Propagates embedding cache metadata through the async call stack without
threading data through every intermediate function signature.

Usage:
    # In the search handler, before calling the search service:
    ctx = SearchEventContext(username="alice", repo_alias="repo1",
                             search_type="semantic", query_text="hello")
    token = _search_event_ctx.set(ctx)
    try:
        result = await search_service.search(...)
        ctx.total_latency_ms = ...
        ctx.result_count = len(result)
    finally:
        _search_event_ctx.reset(token)
        # Enqueue the completed ctx to SearchEventLogWriter
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass
class SearchEventContext:
    """Mutable per-request container for search event data.

    Fields are populated progressively as the request moves through the
    pipeline:
      - username, repo_alias, search_type, query_text: set by the handler
        before search begins.
      - voyage_cache_hit, voyage_cache_mode: set after
        coalesced_query_embedding() returns EmbeddingCacheMetadata.
      - cohere_cache_hit, cohere_cache_mode: same for Cohere provider.
      - total_latency_ms, result_count: set after the search completes.
    """

    username: str
    repo_alias: Optional[str]
    search_type: str
    query_text: str

    # Voyage embedding cache telemetry (None if Voyage not used)
    voyage_cache_hit: Optional[bool] = None
    voyage_cache_mode: Optional[str] = None
    voyage_latency_ms: Optional[int] = None

    # Cohere embedding cache telemetry (None if Cohere not used)
    cohere_cache_hit: Optional[bool] = None
    cohere_cache_mode: Optional[str] = None
    cohere_latency_ms: Optional[int] = None

    # End-of-request totals
    total_latency_ms: int = 0
    result_count: int = 0


# Process-level ContextVar — one slot per concurrent asyncio task / thread.
# Default is None so reads outside a search request never raise LookupError.
_search_event_ctx: ContextVar[Optional[SearchEventContext]] = ContextVar(
    "_search_event_ctx", default=None
)


def get_search_event_ctx() -> Optional[SearchEventContext]:
    """Return the current request's SearchEventContext, or None outside a search."""
    return _search_event_ctx.get()
