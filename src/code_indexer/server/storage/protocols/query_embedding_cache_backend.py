"""QueryEmbeddingCacheBackend Protocol (Story #1105).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class QueryEmbeddingCacheBackend(Protocol):
    """Protocol for query-embedding cache storage (Story #1105).

    Stores float32 little-endian embedding blobs keyed by
    (cache_key, provider, model, dimension).  Supports both SQLite (solo)
    and PostgreSQL (cluster) backends.

    The composite PK prevents cross-provider / cross-model collisions: a
    voyage-code-3 (1024 dims) vector and a cohere embed-v4.0 (1536 dims)
    vector for the *same* query text occupy separate rows.

    The ``last_used`` touch on cache hits is ASYNC/BEST-EFFORT (Bug #1181 Perf Fix #2):
    ``record_hit`` buffers touches in a coalescing in-process dict and a background
    thread drains them via ``touch_last_used_batch`` every ~5 seconds.  The hot query
    path performs ZERO synchronous DB writes on a cache hit.  Approximate LRU is
    acceptable — touches may be coalesced or delayed.
    """

    def lookup(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
    ) -> "Optional[bytes]":
        """Return the stored embedding bytes (float32 LE) or None on miss.

        Args:
            cache_key: SHA-256 hex string of the (normalized) query text.
            provider: Provider name, e.g. 'voyage-ai' or 'cohere'.
            model: Model name, e.g. 'voyage-code-3'.
            dimension: Embedding dimension, e.g. 1024.

        Returns:
            Raw bytes blob (float32 LE) or None if not present.
        """
        ...

    def upsert(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
        embedding: bytes,
        created_at: float,
        last_used: float,
    ) -> bool:
        """Insert or update the embedding row.

        On conflict (cache_key, provider, model, dimension) the existing row
        is updated (embedding + last_used).

        Bug #1536: fails open (never raises) and reports success/failure via
        the return value — True on success, False on failure — so callers
        (QueryEmbeddingCache.record_miss_or_shadow) can count persistent
        write failures instead of that condition being indistinguishable
        from success.

        Args:
            cache_key: SHA-256 hex string of the (normalized) query text.
            provider: Provider name.
            model: Model name.
            dimension: Embedding dimension.
            embedding: Float32 LE bytes blob.
            created_at: Epoch seconds (first write).
            last_used: Epoch seconds (most recent use).

        Returns:
            True on success, False on failure (never raises).
        """
        ...

    def touch_last_used(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
        last_used: float,
    ) -> None:
        """Update last_used for an existing row (kept for direct callers/tests).

        NOTE: The hot cache-hit path uses ``touch_last_used_batch`` via the
        async flusher (Bug #1181 Perf Fix #2).  This single-row method is
        retained for compatibility and direct testing.

        Args:
            cache_key: SHA-256 hex string.
            provider: Provider name.
            model: Model name.
            dimension: Embedding dimension.
            last_used: New last_used epoch seconds.
        """
        ...

    def touch_last_used_batch(
        self,
        items: "List[Tuple[str, str, str, int, float]]",
    ) -> None:
        """Update last_used for multiple rows in a single batch transaction.

        Bug #1181 Perf Fix #2: the async touch flusher drains the coalescing
        buffer by calling this method with all accumulated (cache_key, provider,
        model, dimension, last_used) tuples.  A single transaction reduces WAL
        lock contention on SQLite and uses SET LOCAL synchronous_commit=off on
        PostgreSQL (ephemeral LRU bookkeeping — safe).

        Implementations MUST be fail-open: log a WARNING on any error and never
        raise.  Empty list must be a no-op (no DB round-trip).

        Args:
            items: List of (cache_key, provider, model, dimension, last_used)
                tuples.  May be empty (no-op).
        """
        ...

    def prune_to_max(self, max_entries: int) -> int:
        """Delete rows beyond max_entries, ordered by last_used ASC.

        Args:
            max_entries: Desired maximum number of rows after pruning.

        Returns:
            Number of rows actually deleted.
        """
        ...

    def total_entries(self) -> int:
        """Return the total number of rows in the cache table.

        Returns:
            Integer row count.
        """
        ...

    def clear(self) -> None:
        """Delete all rows from the cache table (used for testing / resets)."""
        ...
