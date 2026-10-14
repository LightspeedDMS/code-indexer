"""PayloadCacheBackend Protocol (Story #504: PayloadCacheBackend Protocol and Backends).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable


@runtime_checkable
class PayloadCacheBackend(Protocol):
    """Protocol for payload cache storage (Story #504).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Stores large content with TTL-based eviction, keyed by a cache handle.
    Each entry may record its creator (``owner``, a username); an entry with
    no owner has no creator and is never served by the generic retrieval
    doors.
    """

    def store(
        self,
        cache_handle: str,
        content: str,
        preview: str,
        ttl_seconds: int,
        node_id: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> None:
        """Store a payload cache entry.

        Args:
            cache_handle: Unique identifier for this cache entry.
            content: Full content to cache.
            preview: Truncated preview of the content.
            ttl_seconds: Time-to-live in seconds.
            node_id: Optional cluster node identifier (NULL in standalone).
            owner: Username of the entry's creator (NULL: no creator).
        """
        ...

    def store_batch(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> None:
        """Store multiple payload cache entries in ONE atomic transaction.

        Bug #1181: Batch all per-query stores to avoid N fsync'd commits.
        The facade (PayloadCache) is the sole handle authority — it generates
        UUID4 handles and passes them in via entries.

        Args:
            entries: List of (cache_handle, content, preview, ttl_seconds) tuples.
            node_id: Optional cluster node identifier (NULL in standalone).
            owner: Username of the creator of every entry in the batch,
                written by the same statement (NULL: no creator).
        """
        ...

    def store_batch_strict(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> None:
        """Store multiple payload cache entries in ONE atomic transaction,
        PROPAGATING any write failure instead of swallowing it.

        Bug #1928 (round 3, P1): identical one-transaction/one-timestamp
        semantics to store_batch() above, but a write failure here MUST
        raise -- never be caught, warning-logged, and silently dropped.
        Used for page-set writes (e.g. xray_truncation's whole-entry
        pages + pages-v1 manifest, all sharing one timestamp/expiry) where
        the caller needs to know the write genuinely failed rather than
        receive a handle pointing at data that was never durably written.

        Args:
            entries: List of (cache_handle, content, preview, ttl_seconds) tuples.
            node_id: Optional cluster node identifier (NULL in standalone).
            owner: Username of the creator of every entry in the batch.

        Raises:
            Whatever the underlying connection/transaction raises on
            failure (backend-specific -- e.g. psycopg errors for
            PostgreSQL, sqlite3 errors for SQLite).
        """
        ...

    def retrieve(self, cache_handle: str) -> Optional[Dict[str, Any]]:
        """Retrieve a cache entry by handle, or None if missing or expired.

        Args:
            cache_handle: Unique identifier for the cache entry.

        Returns:
            Dict with keys: content, preview, created_at, node_id, owner —
            or None if the entry does not exist or has exceeded its TTL.
        """
        ...

    def retrieve_owned(self, cache_handle: str, owner: str) -> Optional[Dict[str, Any]]:
        """Retrieve an entry only when `owner` created it.

        ONE query filtered by handle AND owner: a missing handle, an
        owner-less entry and another user's entry all match no row, so
        their content is never read (same cost as a missing handle).

        Returns:
            Same dict as retrieve(), or None (missing, not owned, expired).
        """
        ...

    def store_expiring(self, cache_handle: str, content: str, ttl_seconds: int) -> None:
        """Insert or replace one entry that lives exactly `ttl_seconds`.

        The entry's creation time comes from the store's own clock (the
        database clock), never the caller's, so every node agrees on when
        it expires. The entry has no creator (owner NULL). Failures
        propagate.
        """
        ...

    def consume(self, cache_handle: str) -> bool:
        """Atomically delete a live (unexpired) entry.

        Returns True only to the single caller whose delete removed the
        entry; every concurrent or later caller, on any node, gets False.
        An expired or missing entry yields False. Liveness is measured on
        the store's own clock, the clock store_expiring() stamps entries
        with. Failures propagate.

        Args:
            cache_handle: Unique identifier for the cache entry.
        """
        ...

    def cleanup_expired(self) -> int:
        """Delete all entries that have exceeded their TTL.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
