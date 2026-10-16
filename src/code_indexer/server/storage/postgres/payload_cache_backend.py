"""
PostgreSQL backend for payload cache storage (Story #504).

Drop-in replacement for PayloadCacheSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the PayloadCacheBackend Protocol (protocols.py).

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required for the payload_cache table. The nullable
``owner`` column (the username of an entry's creator) is added in place on a
table that predates it, and by migration 075.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

_PG_NOW = "NOW()"

# One upsert statement for every write path, so each path writes (and on
# conflict replaces) the same columns, owner included.
_UPSERT_SQL = """
    INSERT INTO payload_cache
        (cache_handle, content, preview, created_at, ttl_seconds, node_id, owner)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (cache_handle) DO UPDATE SET
        content = EXCLUDED.content,
        preview = EXCLUDED.preview,
        created_at = EXCLUDED.created_at,
        ttl_seconds = EXCLUDED.ttl_seconds,
        node_id = EXCLUDED.node_id,
        owner = EXCLUDED.owner
"""

# Entry reads. The owner-filtered one matches no row for a missing handle,
# an owner-less entry or another user's entry, so their content is never
# transferred (same cost as a missing handle).
_SELECT_BY_HANDLE = """
    SELECT content, preview, created_at, ttl_seconds, node_id, owner
    FROM payload_cache
    WHERE cache_handle = %s
"""
_SELECT_BY_HANDLE_AND_OWNER = """
    SELECT content, preview, created_at, ttl_seconds, node_id, owner
    FROM payload_cache
    WHERE cache_handle = %s AND owner = %s
"""


def _live_predicate(now_sql: str) -> str:
    """SQL true while an entry is younger than its TTL -- the one expiry rule
    (consume() keeps what it accepts, cleanup_expired() deletes its
    negation), using exact interval arithmetic. `now_sql` is the SQL for the
    current time (_PG_NOW, the database clock, in production)."""
    return f"({now_sql} - created_at::timestamptz) < make_interval(secs => ttl_seconds)"


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
        """Create the payload_cache table if it does not already exist, and
        add the nullable owner column to a table that predates it (its old
        rows read NULL: no creator)."""
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
                        node_id TEXT,
                        owner TEXT
                    )
                    """
                )
                conn.execute(
                    "ALTER TABLE payload_cache ADD COLUMN IF NOT EXISTS owner TEXT"
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
        owner: Optional[str] = None,
    ) -> None:
        """Store a payload cache entry, replacing any existing entry with the same handle.

        Args:
            cache_handle: Unique identifier for this cache entry.
            content: Full content to cache.
            preview: Truncated preview of the content.
            ttl_seconds: Time-to-live in seconds.
            node_id: Optional cluster node identifier (NULL in standalone).
            owner: Username of the entry's creator (NULL: no creator).
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._pool.connection() as conn:
                # Bug #1181: Relax durability for ephemeral payload_cache writes.
                # SET LOCAL is per-transaction; does not affect users/jobs/migrations.
                conn.execute("SET LOCAL synchronous_commit = off")
                conn.execute(
                    _UPSERT_SQL,
                    (cache_handle, content, preview, now, ttl_seconds, node_id, owner),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("PayloadCachePostgresBackend: store failed: %s", exc)

    def _execute_batch_insert(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str],
<<<<<<< HEAD
=======
        owner: Optional[str],
>>>>>>> upstream/master
    ) -> None:
        """Build rows and run the ONE-transaction batch INSERT. Shared by
        store_batch() (which catches and warning-logs a failure) and
        store_batch_strict() (Bug #1928 round-3 P1, which lets a failure
        propagate) so the SQL/transaction logic is defined exactly once.
<<<<<<< HEAD
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (handle, content, preview, now, ttl_secs, node_id)
=======
        The owner is a column of each row, written by the same statement.
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (handle, content, preview, now, ttl_secs, node_id, owner)
>>>>>>> upstream/master
            for handle, content, preview, ttl_secs in entries
        ]
        with self._pool.connection() as conn:
            # Bug #1181: Relax WAL fsync for ephemeral payload_cache writes.
            # SET LOCAL is per-transaction; does not affect other statement types.
            conn.execute("SET LOCAL synchronous_commit = off")
            # psycopg v3: executemany lives on the cursor, NOT the connection.
            # Using the cursor keeps this in the SAME transaction as the
            # SET LOCAL above and the single commit() below.
            with conn.cursor() as cur:
<<<<<<< HEAD
                cur.executemany(
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
                    rows,
                )
=======
                cur.executemany(_UPSERT_SQL, rows)
>>>>>>> upstream/master
            conn.commit()

    def store_batch(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> None:
        """Store multiple payload cache entries in ONE transaction/commit.

        Bug #1181: Batch all per-query stores to avoid N fsync'd commits per query
        saturating the PostgreSQL WAL write lock at concurrency.

        A write failure here is caught and only warning-logged (best-effort
        caching semantics for the many existing callers of this method --
        see store_batch_strict() below for the propagating variant).

        Args:
            entries: List of (cache_handle, content, preview, ttl_seconds) tuples.
            node_id: Optional cluster node identifier (NULL in standalone).
            owner: Username of the creator of every entry in the batch.
        """
        if not entries:
            return
        try:
<<<<<<< HEAD
            self._execute_batch_insert(entries, node_id)
=======
            self._execute_batch_insert(entries, node_id, owner)
>>>>>>> upstream/master
        except Exception as exc:
            logger.warning("PayloadCachePostgresBackend: store_batch failed: %s", exc)

    def store_batch_strict(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
<<<<<<< HEAD
=======
        owner: Optional[str] = None,
>>>>>>> upstream/master
    ) -> None:
        """Bug #1928 (round 3, P1 -- Codex REJECT): identical write to
        store_batch() above, but PROPAGATES any failure instead of
        swallowing it -- used for page-set writes (e.g. xray_truncation's
        whole-entry pages + pages-v1 manifest) where the caller must
        learn the write genuinely failed rather than receive a handle
        pointing at data that was never durably written.

        Args:
            entries: List of (cache_handle, content, preview, ttl_seconds) tuples.
            node_id: Optional cluster node identifier (NULL in standalone).
<<<<<<< HEAD
=======
            owner: Username of the creator of every entry in the batch.
>>>>>>> upstream/master

        Raises:
            Whatever the underlying psycopg connection/transaction raises.
        """
        if not entries:
            return
<<<<<<< HEAD
        self._execute_batch_insert(entries, node_id)
=======
        self._execute_batch_insert(entries, node_id, owner)
>>>>>>> upstream/master

    def retrieve(self, cache_handle: str) -> Optional[Dict[str, Any]]:
        """Retrieve a cache entry by handle, or None if missing or expired.

        Args:
            cache_handle: Unique identifier for the cache entry.

        Returns:
            Dict with keys: content, preview, created_at, node_id, owner --
            or None if the entry does not exist or has exceeded its TTL.
        """
        return self._fetch_live(_SELECT_BY_HANDLE, (cache_handle,))

    def retrieve_owned(self, cache_handle: str, owner: str) -> Optional[Dict[str, Any]]:
        """Retrieve an entry only when `owner` created it, in ONE query that
        filters by owner: a missing handle, an owner-less entry and another
        user's entry all match no row, so their content is never transferred.

        Returns:
            Same dict as retrieve(), or None (missing, not owned, expired).
        """
        return self._fetch_live(_SELECT_BY_HANDLE_AND_OWNER, (cache_handle, owner))

    def _fetch_live(
        self, sql: str, params: Tuple[str, ...]
    ) -> Optional[Dict[str, Any]]:
        """Run one entry SELECT and return the live entry, or None when no
        row matched, the row has exceeded its TTL, or the read failed."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(sql, params).fetchone()
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
            "owner": row[5],
        }

    def store_expiring(self, cache_handle: str, content: str, ttl_seconds: int) -> None:
        """Insert or replace one entry living exactly `ttl_seconds`, stamped
        by the database clock (see PayloadCacheBackend.store_expiring).
        The entry has no creator (owner NULL). Errors propagate."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO payload_cache
                    (cache_handle, content, preview, created_at, ttl_seconds,
                     node_id, owner)
                VALUES (
                    %s, %s, '',
                    to_char(now() AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
                    %s, NULL, NULL
                )
                ON CONFLICT (cache_handle) DO UPDATE SET
                    content = EXCLUDED.content,
                    preview = EXCLUDED.preview,
                    created_at = EXCLUDED.created_at,
                    ttl_seconds = EXCLUDED.ttl_seconds,
                    node_id = EXCLUDED.node_id,
                    owner = EXCLUDED.owner
                """,
                (cache_handle, content, ttl_seconds),
            )
            conn.commit()

    def consume(self, cache_handle: str) -> bool:
        """Atomically delete a live entry; True only for the one caller
        whose DELETE removed it (see PayloadCacheBackend.consume).

        A concurrent DELETE of the same row waits on the row lock and then
        matches nothing, so exactly one caller sees rowcount 1. Age is
        measured on the database clock, the clock store_expiring() stamps
        entries with. Errors propagate.
        """
        with self._pool.connection() as conn:
            result = conn.execute(
                f"""
                DELETE FROM payload_cache
                WHERE cache_handle = %s AND {_live_predicate(_PG_NOW)}
                """,
                (cache_handle,),
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()
        return deleted == 1

    def cleanup_expired(self) -> int:
        """Delete all entries that have exceeded their TTL.

        Returns:
            Number of rows deleted.
        """
        try:
            with self._pool.connection() as conn:
                result = conn.execute(
                    f"DELETE FROM payload_cache WHERE NOT ({_live_predicate(_PG_NOW)})"
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
