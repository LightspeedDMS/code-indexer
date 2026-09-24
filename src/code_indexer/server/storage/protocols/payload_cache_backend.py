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
    """

    def store(
        self,
        cache_handle: str,
        content: str,
        preview: str,
        ttl_seconds: int,
        node_id: Optional[str] = None,
    ) -> None:
        """Store a payload cache entry.

        Args:
            cache_handle: Unique identifier for this cache entry.
            content: Full content to cache.
            preview: Truncated preview of the content.
            ttl_seconds: Time-to-live in seconds.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        ...

    def store_batch(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
    ) -> None:
        """Store multiple payload cache entries in ONE atomic transaction.

        Bug #1181: Batch all per-query stores to avoid N fsync'd commits.
        The facade (PayloadCache) is the sole handle authority — it generates
        UUID4 handles and passes them in via entries.

        Args:
            entries: List of (cache_handle, content, preview, ttl_seconds) tuples.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        ...

    def store_batch_strict(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
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
            Dict with keys: content, preview, created_at, node_id — or None
            if the entry does not exist or has exceeded its TTL.
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
