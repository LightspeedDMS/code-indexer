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

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
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

    Bug #1813 (DEFECT 2): the omni/multi-repo fan-out
    (multi/multi_search_service.py) submits one search task per repo to a
    shared ThreadPoolExecutor via contextvars.copy_context().run(...) -- a
    documented, correct pattern for propagating correlation_id across the
    executor boundary. But a context copy is SHALLOW: every fan-out
    worker's copy still resolves to the IDENTICAL SearchEventContext
    instance. Two repos using the same provider (e.g. both Voyage) whose
    embed calls complete concurrently can therefore race, unsynchronized,
    on this SAME provider's (cache_hit, cache_mode, latency_ms) triple --
    record_provider_cache_fields() below is the single, lock-protected
    write path both call sites (filesystem_vector_store.py,
    search_service.py) MUST use instead of assigning the three fields
    directly, so a concurrent writer can never observe (or leave behind) a
    torn combination mixing two different calls' outcomes.
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

    # Bug #1813 (DEFECT 2): guards record_provider_cache_fields() so a
    # provider's (hit, mode, latency) triple is always written atomically.
    # Excluded from repr/eq (a lock has no meaningful value semantics).
    _provider_fields_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record_provider_cache_fields(
        self,
        provider_name: str,
        *,
        cache_hit: Optional[bool],
        cache_mode: Optional[str],
        latency_ms: Optional[int],
    ) -> None:
        """Atomically record one provider's cache-hit/-mode/-latency triple.

        Bug #1813 (DEFECT 2): the single, lock-protected write path for the
        (voyage_cache_hit, voyage_cache_mode, voyage_latency_ms) /
        (cohere_cache_hit, cohere_cache_mode, cohere_latency_ms) triples.
        Concurrent callers (e.g. two omni fan-out repos sharing this SAME
        context via contextvars.copy_context()'s shallow-copy semantics)
        serialize here -- each call's three fields are written as one
        atomic group, so the final state always reflects exactly one
        caller's outcome (the last to finish), never a scrambled mix of
        two different calls' fields.
        """
        with self._provider_fields_lock:
            if "cohere" in provider_name.lower():
                self.cohere_cache_hit = cache_hit
                self.cohere_cache_mode = cache_mode
                self.cohere_latency_ms = latency_ms
            else:
                self.voyage_cache_hit = cache_hit
                self.voyage_cache_mode = cache_mode
                self.voyage_latency_ms = latency_ms


# Process-level ContextVar — one slot per concurrent asyncio task / thread.
# Default is None so reads outside a search request never raise LookupError.
_search_event_ctx: ContextVar[Optional[SearchEventContext]] = ContextVar(
    "_search_event_ctx", default=None
)


def get_search_event_ctx() -> Optional[SearchEventContext]:
    """Return the current request's SearchEventContext, or None outside a search."""
    return _search_event_ctx.get()
