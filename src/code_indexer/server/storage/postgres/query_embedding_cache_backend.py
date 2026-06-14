"""
PostgreSQL backend for query-embedding cache storage (Story #1105).

Drop-in replacement for QueryEmbeddingCacheSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the QueryEmbeddingCacheBackend
Protocol (protocols.py).

Embeddings are stored as bytea (raw float32 little-endian bytes), identical to
the SQLite BLOB representation.
"""

from __future__ import annotations

import logging
from typing import Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class QueryEmbeddingCachePostgresBackend:
    """
    PostgreSQL backend for query-embedding cache storage.

    Satisfies the QueryEmbeddingCacheBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations use auto-commit via the pool's connection context manager.
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
        """Create the query_embedding_cache table and index if absent."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS query_embedding_cache (
                        cache_key  TEXT    NOT NULL,
                        provider   TEXT    NOT NULL,
                        model      TEXT    NOT NULL,
                        dimension  INTEGER NOT NULL,
                        embedding  BYTEA   NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL,
                        last_used  DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (cache_key, provider, model, dimension)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_qec_last_used
                    ON query_embedding_cache (last_used)
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "QueryEmbeddingCachePostgresBackend: schema setup failed: %s", exc
            )

    def lookup(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
    ) -> Optional[bytes]:
        """Return the stored embedding bytes (float32 LE) or None on miss."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    SELECT embedding FROM query_embedding_cache
                    WHERE cache_key = %s
                      AND provider  = %s
                      AND model     = %s
                      AND dimension = %s
                    """,
                    (cache_key, provider, model, dimension),
                ).fetchone()
        except Exception as exc:
            logger.warning("QueryEmbeddingCachePostgresBackend: lookup failed: %s", exc)
            return None

        if row is None:
            return None
        val = row[0]
        return bytes(val) if not isinstance(val, bytes) else val

    def upsert(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
        embedding: bytes,
        created_at: float,
        last_used: float,
    ) -> None:
        """Insert or update the embedding row."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO query_embedding_cache
                        (cache_key, provider, model, dimension, embedding,
                         created_at, last_used)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key, provider, model, dimension) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        last_used = EXCLUDED.last_used
                    """,
                    (
                        cache_key,
                        provider,
                        model,
                        dimension,
                        embedding,
                        created_at,
                        last_used,
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("QueryEmbeddingCachePostgresBackend: upsert failed: %s", exc)

    def touch_last_used(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
        last_used: float,
    ) -> None:
        """Update last_used for an existing row."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    UPDATE query_embedding_cache
                    SET last_used = %s
                    WHERE cache_key = %s
                      AND provider  = %s
                      AND model     = %s
                      AND dimension = %s
                    """,
                    (last_used, cache_key, provider, model, dimension),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "QueryEmbeddingCachePostgresBackend: touch_last_used failed: %s", exc
            )

    def prune_to_max(self, max_entries: int) -> int:
        """Delete rows beyond max_entries ordered by last_used ASC (deterministic tie-break).

        Uses a ctid-based DELETE with OFFSET so the entire eviction is a single
        atomic statement.  The secondary sort ensures deterministic eviction when
        last_used values are identical:
            ORDER BY last_used ASC, created_at ASC, cache_key ASC,
                     provider ASC, model ASC, dimension ASC

        Minimum bound: ``max_entries < 100`` falls back to the safe default of 100.
        This prevents accidental erasure of the entire cache when a misconfigured
        or untested value is passed.

        Args:
            max_entries: Maximum rows to retain.  Values < 100 are clamped to 100.

        Returns:
            Number of rows actually deleted (0 when already within cap).
        """
        _MIN_CAP = 100
        effective_max = max_entries if max_entries >= _MIN_CAP else _MIN_CAP

        try:
            with self._pool.connection() as conn:
                result = conn.execute(
                    """
                    DELETE FROM query_embedding_cache
                    WHERE ctid IN (
                        SELECT ctid FROM query_embedding_cache
                        ORDER BY last_used ASC,
                                 created_at ASC,
                                 cache_key ASC,
                                 provider ASC,
                                 model ASC,
                                 dimension ASC
                        OFFSET %s
                    )
                    """,
                    (effective_max,),
                )
                deleted = int(result.rowcount) if result.rowcount else 0
                conn.commit()
                return deleted
        except Exception as exc:
            logger.warning(
                "QueryEmbeddingCachePostgresBackend: prune_to_max failed: %s", exc
            )
            return 0

    def total_entries(self) -> int:
        """Return the total number of rows in the cache table."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM query_embedding_cache"
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            logger.warning(
                "QueryEmbeddingCachePostgresBackend: total_entries failed: %s", exc
            )
            return 0

    def clear(self) -> None:
        """Delete all rows from the cache table."""
        try:
            with self._pool.connection() as conn:
                conn.execute("DELETE FROM query_embedding_cache")
                conn.commit()
        except Exception as exc:
            logger.warning("QueryEmbeddingCachePostgresBackend: clear failed: %s", exc)

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
