"""Interim in-process query-embedding (vector) cache — fork-local.

Caches ``query-text -> embedding vector`` so repeated queries skip the VoyageAI
embedding API call. For a support-KB workload the same "how-to" questions recur
constantly across dealers, and the embedding call is the dominant per-query cost
AND the one shared external bottleneck that horizontal pod scaling does NOT
relieve (more pods == proportionally more VoyageAI calls). A cache hit reuses the
vector via the existing ``precomputed_query_vector`` path, avoiding both the API
round-trip and its rate-limit/$ pressure.

Design:
  * Key = ``provider \x1f model \x1f normalized-query`` so different providers or
    models never collide, and a model change naturally invalidates (new keyspace).
  * Value = the embedding vector. Embeddings are deterministic for a fixed
    (provider, model, text), so entries never go semantically stale — the TTL and
    LRU cap exist only to bound memory and cap staleness after a model swap.
  * Bounded LRU + TTL, thread-safe, process-local (per pod), default-OFF.

NOTE (fork): upstream/master ships a richer ``QueryEmbeddingCache``
(``services/query_embedding_cache.py``) with anchor-token normalization,
per-provider shadow modes, request coalescing, and a Postgres-backed cross-pod
cache. This module is a deliberately small in-process interim for this fork until
upstream is merged; replace it with upstream's implementation at that point. The
distinct name (``QueryVectorCache`` / ``query_vector_cache``) keeps the two from
colliding on merge.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

# Field separator for cache keys — a control char that cannot appear in a
# provider name, model name, or user query, so keys are unambiguous.
_SEP = "\x1f"


class QueryVectorCache:
    """Thread-safe bounded LRU+TTL cache of query text -> embedding vector."""

    def __init__(
        self,
        max_entries: int,
        ttl_seconds: float,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max(0, int(max_entries))
        self._ttl = float(ttl_seconds)
        self._time = time_fn
        self._lock = threading.Lock()
        # key -> (expires_at, vector); ordered by recency of use (LRU at front).
        self._entries: "OrderedDict[str, Tuple[float, List[float]]]" = OrderedDict()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def make_key(provider: str, model: str, query: str) -> str:
        """Build a cache key. Whitespace is collapsed so "foo  bar" and "foo bar"
        share an entry; case is PRESERVED because embeddings are case-sensitive.
        """
        normalized = " ".join(query.split())
        return f"{provider}{_SEP}{model}{_SEP}{normalized}"

    def get(self, key: str) -> Optional[List[float]]:
        """Return the cached vector for ``key`` or None (miss/expired)."""
        if self._max == 0:
            return None
        now = self._time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            expires_at, vector = entry
            if now >= expires_at:
                del self._entries[key]
                self._misses += 1
                return None
            self._entries.move_to_end(key)  # mark most-recently-used
            self._hits += 1
            return vector

    def put(self, key: str, vector: List[float]) -> None:
        """Cache ``vector`` under ``key``, evicting the LRU entry past the cap."""
        if self._max == 0:
            return
        expires_at = self._time() + self._ttl
        with self._lock:
            self._entries[key] = (expires_at, vector)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)  # evict least-recently-used

    def stats(self) -> Dict[str, int]:
        """Return hit/miss/size counters (for observability)."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._entries),
            }


# ---------------------------------------------------------------------------
# Process-wide singleton (one cache per pod), configured at startup.
# ---------------------------------------------------------------------------

_cache: Optional[QueryVectorCache] = None
_cache_lock = threading.Lock()


def configure_query_vector_cache(
    enabled: bool, max_entries: int, ttl_seconds: float
) -> None:
    """Install (or clear) the process-wide query-vector cache.

    Called once from app wiring. When ``enabled`` is False the singleton is None
    and the query path behaves exactly as before (zero overhead).
    """
    global _cache
    with _cache_lock:
        _cache = (
            QueryVectorCache(max_entries=max_entries, ttl_seconds=ttl_seconds)
            if enabled
            else None
        )


def get_query_vector_cache() -> Optional[QueryVectorCache]:
    """Return the configured cache, or None when disabled/unconfigured."""
    return _cache
