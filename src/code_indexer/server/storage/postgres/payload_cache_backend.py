"""
PostgreSQL backend for payload cache storage (Story #504).

Drop-in replacement for PayloadCacheSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the PayloadCacheBackend Protocol (protocols.py).

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required for the payload_cache table.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class PayloadCachePostgresBackend:
    """
    PostgreSQL backend for payload cache storage.

    Satisfies the PayloadCacheBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure the table exists.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the payload_cache table and indexes if they do not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS payload_cache (
                        cache_handle TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        preview TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        ttl_seconds INTEGER NOT NULL,
                        node_id TEXT
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("PayloadCachePostgresBackend: schema setup failed: %s", exc)

    def store(
        self,
        cache_handle: str,
        content: str,
        preview: str,
        ttl_seconds: int,
        node_id: Optional[str] = None,
    ) -> None:
        """Store a payload cache entry, replacing any existing entry with the same handle.

        Args:
            cache_handle: Unique identifier for this cache entry.
            content: Full content to cache.
            preview: Truncated preview of the content.
            ttl_seconds: Time-to-live in seconds.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO payload_cache
                        (cache_handle, content, preview, created_at, ttl_seconds, node_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_handle) DO UPDATE SET
                        content = EXCLUDED.content,
                        preview = EXCLUDED.preview,
                        created_at = EXCLUDED.created_at,
                        ttl_seconds = EXCLUDED.ttl_seconds,
                        node_id = EXCLUDED.node_id
                    """,
                    (cache_handle, content, preview, now, ttl_seconds, node_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("PayloadCachePostgresBackend: store failed: %s", exc)

    def retrieve(self, cache_handle: str) -> Optional[Dict[str, Any]]:
        """Retrieve a cache entry by handle, or None if missing or expired.

        Args:
            cache_handle: Unique identifier for the cache entry.

        Returns:
            Dict with keys: content, preview, created_at, node_id — or None
            if the entry does not exist or has exceeded its TTL.
        """
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    SELECT content, preview, created_at, ttl_seconds, node_id
                    FROM payload_cache
                    WHERE cache_handle = %s
                    """,
                    (cache_handle,),
                ).fetchone()
        except Exception as exc:
            logger.warning("PayloadCachePostgresBackend: retrieve failed: %s", exc)
            return None

        if row is None:
            return None

        created_at_str: str = row[2]
        ttl_secs: int = row[3]

        # Check TTL expiry
        try:
            created_at = datetime.fromisoformat(created_at_str)
            now = datetime.now(timezone.utc)
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            elapsed = (now - created_at).total_seconds()
            if elapsed > ttl_secs:
                return None
        except (ValueError, TypeError):
            return None

        return {
            "content": row[0],
            "preview": row[1],
            "created_at": created_at_str,
            "node_id": row[4],
        }

    def cleanup_expired(self) -> int:
        """Delete all entries that have exceeded their TTL.

        Returns:
            Number of rows deleted.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._pool.connection() as conn:
                result = conn.execute(
                    """
                    DELETE FROM payload_cache
                    WHERE (
                        EXTRACT(EPOCH FROM (NOW() - created_at::timestamptz))
                    ) > ttl_seconds
                    """,
                    (now,),
                )
                deleted = int(result.rowcount) if result.rowcount else 0
                conn.commit()
        except Exception as exc:
            logger.warning(
                "PayloadCachePostgresBackend: cleanup_expired failed: %s", exc
            )
            return 0

        if deleted:
            logger.debug(
                "PayloadCachePostgresBackend: cleaned up %d expired cache entries",
                deleted,
            )
        return deleted

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
