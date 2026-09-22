"""
SQLite backend for query-embedding cache storage (Story #1105).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class QueryEmbeddingCacheSqliteBackend:
    """SQLite backend for query-embedding cache storage (Story #1105).

    Stores float32 little-endian embedding blobs keyed by
    (cache_key, provider, model, dimension).  Uses a dedicated DB file
    so large BLOB writes do not contend with main server state.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize backend and create table/index if absent.

        Args:
            db_path: Path to SQLite database file
                     (e.g. ~/.cidx-server/data/query_embedding_cache.db).
        """
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

        # WAL mode must be set outside any transaction.
        conn = self._conn_manager.get_connection()
        conn.execute("PRAGMA journal_mode=WAL")

        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the query_embedding_cache table and index if absent."""

        # conn: Any -- matches DatabaseConnectionManager.execute_atomic's
        # generic Callable[[Any], T] callback signature (same annotation
        # used unchanged throughout every backend in this module).
        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_embedding_cache (
                    cache_key  TEXT    NOT NULL,
                    provider   TEXT    NOT NULL,
                    model      TEXT    NOT NULL,
                    dimension  INTEGER NOT NULL,
                    embedding  BLOB    NOT NULL,
                    created_at REAL    NOT NULL,
                    last_used  REAL    NOT NULL,
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

        self._conn_manager.execute_atomic(operation)

    def lookup(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
    ) -> Optional[bytes]:
        """Return the stored embedding bytes (float32 LE) or None on miss."""
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            """
            SELECT embedding FROM query_embedding_cache
            WHERE cache_key = ? AND provider = ? AND model = ? AND dimension = ?
            """,
            (cache_key, provider, model, dimension),
        ).fetchone()
        if row is None:
            return None
        return bytes(row[0])

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
        """Insert or update the embedding row (upserts on composite PK conflict).

        Bug #1536: fails open at the backend layer (mirrors
        QueryEmbeddingCachePostgresBackend's already-fail-open upsert()) —
        a write failure (e.g. an OperationalError from the 30s busy-timeout
        expiring under writer contention on this dedicated db file) is
        logged as a WARNING and reported via a `False` return, never raised.
        The caller (QueryEmbeddingCache.record_miss_or_shadow) uses the
        return value to count persistent failures
        (write_failures_since_start()) rather than relying solely on an
        exception, since a future/alternate backend implementation might
        fail open the same way this Postgres sibling already does.

        Returns:
            True on success, False on failure (never raises).
        """

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT INTO query_embedding_cache
                    (cache_key, provider, model, dimension, embedding, created_at, last_used)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cache_key, provider, model, dimension) DO UPDATE SET
                    embedding  = excluded.embedding,
                    last_used  = excluded.last_used
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

        try:
            self._conn_manager.execute_atomic(operation)
            return True
        except Exception as exc:  # noqa: BLE001 -- fail-open, never raise
            logger.warning(
                "QueryEmbeddingCacheSqliteBackend: upsert failed: %s",
                exc,
                exc_info=True,
            )
            return False

    def touch_last_used(
        self,
        cache_key: str,
        provider: str,
        model: str,
        dimension: int,
        last_used: float,
    ) -> None:
        """Update last_used for an existing row."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                UPDATE query_embedding_cache
                SET last_used = ?
                WHERE cache_key = ? AND provider = ? AND model = ? AND dimension = ?
                """,
                (last_used, cache_key, provider, model, dimension),
            )

        self._conn_manager.execute_atomic(operation)

    def touch_last_used_batch(
        self,
        items: List[Tuple[str, str, str, int, float]],
    ) -> None:
        """Update last_used for multiple rows in a single atomic transaction.

        Bug #1181 Perf Fix #2: drains the async touch flusher's coalescing buffer
        in one executemany call, avoiding per-hit WAL lock contention.

        Args:
            items: List of (cache_key, provider, model, dimension, last_used) tuples.
                Empty list is a no-op.
        """
        if not items:
            return

        # Reorder to (last_used, cache_key, provider, model, dimension) for the UPDATE
        params = [
            (last_used, cache_key, provider, model, dimension)
            for cache_key, provider, model, dimension, last_used in items
        ]

        def operation(conn: Any) -> None:
            conn.executemany(
                """
                UPDATE query_embedding_cache
                SET last_used = ?
                WHERE cache_key = ? AND provider = ? AND model = ? AND dimension = ?
                """,
                params,
            )

        self._conn_manager.execute_atomic(operation)

    def prune_to_max(self, max_entries: int) -> int:
        """Delete rows beyond max_entries ordered by last_used ASC (deterministic tie-break).

        Pure primitive — prunes to exactly max_entries rows.  The >=100 safe floor
        is enforced by the caller at config resolution
        (QueryEmbeddingCache._resolve_max_entries).

        Uses a rowid-based DELETE with OFFSET so the entire eviction is a single
        atomic statement.  The secondary sort ensures deterministic eviction when
        last_used values are identical:
            ORDER BY last_used ASC, created_at ASC, cache_key ASC,
                     provider ASC, model ASC, dimension ASC

        Args:
            max_entries: Maximum rows to retain.  Caller is responsible for
                         applying any minimum floor before passing this value.

        Returns:
            Number of rows actually deleted (0 when already within cap).
        """
        # No local range validation: this is a pure primitive whose only
        # caller (QueryEmbeddingCache._resolve_max_entries) already
        # enforces a >=100 floor before invoking it (see docstring above);
        # duplicating that validation here is out of scope for this
        # verbatim relocation (issue #1935 Part 1).
        result: List[int] = [0]

        def operation(conn: Any) -> None:
            total_row = conn.execute(
                "SELECT COUNT(*) FROM query_embedding_cache"
            ).fetchone()
            total = total_row[0] if total_row else 0

            excess = total - max_entries
            if excess <= 0:
                result[0] = 0
                return

            # Delete the oldest ``excess`` rows — the ones with the smallest
            # last_used values.  Secondary sort columns break ties deterministically
            # so the eviction set is stable across concurrent callers.
            cursor = conn.execute(
                """
                DELETE FROM query_embedding_cache
                WHERE rowid IN (
                    SELECT rowid FROM query_embedding_cache
                    ORDER BY last_used ASC,
                             created_at ASC,
                             cache_key ASC,
                             provider ASC,
                             model ASC,
                             dimension ASC
                    LIMIT :excess
                )
                """,
                {"excess": excess},
            )
            result[0] = cursor.rowcount if cursor.rowcount is not None else 0

        self._conn_manager.execute_atomic(operation)
        return result[0]

    def total_entries(self) -> int:
        """Return the total number of rows in the cache table."""
        conn = self._conn_manager.get_connection()
        row = conn.execute("SELECT COUNT(*) FROM query_embedding_cache").fetchone()
        return row[0] if row else 0

    def select_recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the most-recently-used rows as metadata dicts (NO embedding vectors).

        Story #1149: admin cache-sample readout.  Returns recent rows ordered by
        last_used DESC so callers can verify key shape without direct DB access.
        NEVER includes the embedding column — no vectors, no secrets.

        Args:
            limit: Maximum number of rows to return (default 10).

        Returns:
            List of dicts with keys: cache_key, provider, model, dimension, key_length.
            key_length is len(cache_key) computed DB-side for efficiency.
            Empty list on any backend error (fail-open).
        """
        try:
            conn = self._conn_manager.get_connection()
            rows = conn.execute(
                """
                SELECT cache_key, provider, model, dimension,
                       LENGTH(cache_key) AS key_length
                FROM query_embedding_cache
                ORDER BY last_used DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "cache_key": row[0],
                    "provider": row[1],
                    "model": row[2],
                    "dimension": row[3],
                    "key_length": row[4],
                }
                for row in rows
            ]
        except Exception:
            logger.warning(
                "QueryEmbeddingCacheSqliteBackend: select_recent failed (fail-open)",
                exc_info=True,
            )
            return []

    def clear(self) -> None:
        """Delete all rows from the cache table."""

        def operation(conn: Any) -> None:
            conn.execute("DELETE FROM query_embedding_cache")

        self._conn_manager.execute_atomic(operation)

    def clear_all(self) -> None:
        """Delete all rows from the cache table (AC3 named method).

        Idempotent: clearing an already-empty table is a no-op success.
        """
        self.clear()
