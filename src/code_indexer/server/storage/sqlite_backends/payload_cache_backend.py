"""
SQLite backend for payload cache storage (Story #504).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
import os
<<<<<<< HEAD
=======
import sqlite3
>>>>>>> upstream/master
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

<<<<<<< HEAD
=======
_MS_PER_DAY = 86_400_000
_MS_PER_SECOND = 1000
_SQLITE_NOW = "'now'"


def _epoch_ms(time_sql: str) -> str:
    """SQL for a timestamp as whole INTEGER milliseconds. julianday() accepts
    every stored created_at form (ISO-8601, any fractional digits, '+00:00'
    or 'Z') and keeps millisecond precision; rounding removes its
    floating-point error, so the result is exact."""
    return f"CAST(ROUND(julianday({time_sql}) * {_MS_PER_DAY}) AS INTEGER)"


def _live_predicate(now_sql: str) -> str:
    """SQL true while an entry is younger than its TTL -- the one expiry rule
    (consume() keeps what it accepts, cleanup_expired() deletes its
    negation). Both timestamps are integer milliseconds before they are
    compared. `now_sql` is the SQL for the current time (_SQLITE_NOW in
    production)."""
    return (
        f"{_epoch_ms(now_sql)} - {_epoch_ms('created_at')}"
        f" < ttl_seconds * {_MS_PER_SECOND}"
    )


def ensure_owner_column(conn: sqlite3.Connection) -> None:
    """Add the nullable ``owner`` column to an existing ``payload_cache``
    table that predates it (rows already there read NULL: no creator).

    Several workers can open the same file at once; the loser of that race
    gets "duplicate column name", which means the column now exists. Any
    other error propagates so startup fails loudly.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(payload_cache)")}
    if "owner" in columns:
        return
    try:
        conn.execute("ALTER TABLE payload_cache ADD COLUMN owner TEXT")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise

>>>>>>> upstream/master

class PayloadCacheSqliteBackend:
    """
    SQLite backend for payload cache storage (Story #504).

    Stores large content with TTL-based eviction, keyed by a unique cache handle.
    Uses a dedicated payload_cache.db file to isolate cache writes from other server state.

    Includes a node_id column for cluster support.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend and create the payload_cache table if it does not exist.

        Args:
            db_path: Path to SQLite database file
                     (e.g. ~/.cidx-server/data/payload_cache.db).
        """
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

        # Enable WAL mode outside any transaction (PRAGMA cannot run inside BEGIN).
        conn = self._conn_manager.get_connection()
        conn.execute("PRAGMA journal_mode=WAL")

        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the payload_cache table if it does not already exist."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payload_cache (
                    cache_handle TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
<<<<<<< HEAD
                    node_id TEXT
                )
                """
            )
=======
                    node_id TEXT,
                    owner TEXT
                )
                """
            )
            ensure_owner_column(conn)
>>>>>>> upstream/master

        self._conn_manager.execute_atomic(operation)

    def store(
        self,
        cache_handle: str,
        content: str,
        preview: str,
        ttl_seconds: int,
        node_id: Optional[str] = None,
<<<<<<< HEAD
=======
        owner: Optional[str] = None,
>>>>>>> upstream/master
    ) -> None:
        """Store a payload cache entry, replacing any existing entry with the same handle.

        Args:
            cache_handle: Unique identifier for this cache entry.
            content: Full content to cache.
            preview: Truncated preview of the content.
            ttl_seconds: Time-to-live in seconds.
            node_id: Optional cluster node identifier (NULL in standalone).
<<<<<<< HEAD
=======
            owner: Username of the entry's creator (NULL: no creator).
>>>>>>> upstream/master
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO payload_cache
<<<<<<< HEAD
                    (cache_handle, content, preview, created_at, ttl_seconds, node_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cache_handle, content, preview, now, ttl_seconds, node_id),
=======
                    (cache_handle, content, preview, created_at, ttl_seconds,
                     node_id, owner)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (cache_handle, content, preview, now, ttl_seconds, node_id, owner),
>>>>>>> upstream/master
            )

        self._conn_manager.execute_atomic(operation)

    def store_batch(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
<<<<<<< HEAD
=======
        owner: Optional[str] = None,
>>>>>>> upstream/master
    ) -> None:
        """Store multiple payload cache entries in ONE atomic transaction.

        Bug #1181: Batch all per-query stores into a single commit to avoid
        N fsync'd commits per query saturating the WAL write lock.

        Signature unified with PayloadCachePostgresBackend.store_batch so the
        facade (PayloadCache) can delegate to either backend identically.
        The facade is the sole handle authority — it generates UUID4 handles
        and passes them in via entries.

        Args:
            entries: List of (cache_handle, content, preview, ttl_seconds) tuples.
                     Handles are pre-generated by the facade.
            node_id: Optional cluster node identifier (NULL in standalone).
<<<<<<< HEAD
=======
            owner: Username of the creator of every entry in the batch
                   (NULL: no creator). Written in the same statement.
>>>>>>> upstream/master

        Returns:
            None (handles are pre-provided by the caller).
        """
        if not entries:
            return

        now = datetime.now(timezone.utc).isoformat()
        rows = [
<<<<<<< HEAD
            (handle, content, preview, now, ttl_secs, node_id)
=======
            (handle, content, preview, now, ttl_secs, node_id, owner)
>>>>>>> upstream/master
            for handle, content, preview, ttl_secs in entries
        ]

        def operation(conn: Any) -> None:
            conn.executemany(
                """
                INSERT OR REPLACE INTO payload_cache
<<<<<<< HEAD
                    (cache_handle, content, preview, created_at, ttl_seconds, node_id)
                VALUES (?, ?, ?, ?, ?, ?)
=======
                    (cache_handle, content, preview, created_at, ttl_seconds,
                     node_id, owner)
                VALUES (?, ?, ?, ?, ?, ?, ?)
>>>>>>> upstream/master
                """,
                rows,
            )

        self._conn_manager.execute_atomic(operation)

    def store_batch_strict(
        self,
        entries: List[Tuple[str, str, str, int]],
        node_id: Optional[str] = None,
<<<<<<< HEAD
=======
        owner: Optional[str] = None,
>>>>>>> upstream/master
    ) -> None:
        """Bug #1928 (round 3, P1): identical write to store_batch() above,
        but the Protocol contract requires PROPAGATING any failure. This
        backend's store_batch() already propagates (execute_atomic rolls
        back and re-raises on any error), so this simply delegates to it.
        Exists so the PayloadCacheBackend Protocol offers ONE
        consistently-named "never swallows" method across both backends
        -- only PayloadCachePostgresBackend needed distinct logic (its
        store_batch() catches the failure and only warning-logs it).
        """
<<<<<<< HEAD
        self.store_batch(entries, node_id=node_id)
=======
        self.store_batch(entries, node_id=node_id, owner=owner)
>>>>>>> upstream/master

    def retrieve(self, cache_handle: str) -> Optional[Dict[str, Any]]:
        """Retrieve a cache entry by handle, or None if missing or expired.

        Args:
            cache_handle: Unique identifier for the cache entry.

        Returns:
<<<<<<< HEAD
            Dict with keys: content, preview, created_at, node_id — or None
            if the entry does not exist or has exceeded its TTL.
=======
            Dict with keys: content, preview, created_at, node_id, owner --
            or None if the entry does not exist or has exceeded its TTL.
>>>>>>> upstream/master
        """
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            """
<<<<<<< HEAD
            SELECT cache_handle, content, preview, created_at, ttl_seconds, node_id
=======
            SELECT cache_handle, content, preview, created_at, ttl_seconds,
                   node_id, owner
>>>>>>> upstream/master
            FROM payload_cache
            WHERE cache_handle = ?
            """,
            (cache_handle,),
        ).fetchone()
<<<<<<< HEAD

=======
        return self._live_entry(row)

    def retrieve_owned(self, cache_handle: str, owner: str) -> Optional[Dict[str, Any]]:
        """Retrieve an entry only when `owner` created it, in ONE query that
        filters by owner: a missing handle, an owner-less entry and another
        user's entry all match no row, so their content is never read.

        Returns:
            Same dict as retrieve(), or None (missing, not owned, expired).
        """
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            """
            SELECT cache_handle, content, preview, created_at, ttl_seconds,
                   node_id, owner
            FROM payload_cache
            WHERE cache_handle = ? AND owner = ?
            """,
            (cache_handle, owner),
        ).fetchone()
        return self._live_entry(row)

    @staticmethod
    def _live_entry(row: Any) -> Optional[Dict[str, Any]]:
        """The entry dict for a selected row, or None when there is no row
        or it has exceeded its TTL."""
>>>>>>> upstream/master
        if row is None:
            return None

        created_at_str: str = row[3]
        ttl_secs: int = row[4]

        # Check TTL expiry
        try:
            created_at = datetime.fromisoformat(created_at_str)
            now = datetime.now(timezone.utc)
            # Ensure created_at is timezone-aware for comparison
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            elapsed = (now - created_at).total_seconds()
            if elapsed > ttl_secs:
                return None
        except (ValueError, TypeError):
            # Pre-existing, intentional: an unparseable created_at means a
            # corrupted/unreadable row, treated as a cache miss (not an
            # error) rather than raising -- matches this method's
            # original, already-shipped, already-tested contract.
            return None

        return {
            "content": row[1],
            "preview": row[2],
            "created_at": created_at_str,
            "node_id": row[5],
<<<<<<< HEAD
        }

    def cleanup_expired(self) -> int:
        """Delete all entries that have exceeded their TTL.
=======
            "owner": row[6],
        }

    def store_expiring(self, cache_handle: str, content: str, ttl_seconds: int) -> None:
        """Insert or replace one entry living exactly `ttl_seconds`, stamped
        by the database clock (see PayloadCacheBackend.store_expiring)."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO payload_cache
                    (cache_handle, content, preview, created_at, ttl_seconds, node_id)
                VALUES (?, ?, '', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), ?, NULL)
                """,
                (cache_handle, content, ttl_seconds),
            )

        self._conn_manager.execute_atomic(operation)

    def consume(self, cache_handle: str) -> bool:
        """Atomically delete a live entry; True only for the one caller
        whose single DELETE removed it (see PayloadCacheBackend.consume).

        Age is measured at sub-second precision on the database clock, the
        clock store_expiring() stamps entries with.
        """

        def operation(conn: Any) -> int:
            cursor = conn.execute(
                f"""
                DELETE FROM payload_cache
                WHERE cache_handle = ? AND {_live_predicate(_SQLITE_NOW)}
                """,
                (cache_handle,),
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        return deleted == 1

    def cleanup_expired(self) -> int:
        """Delete all entries that have exceeded their TTL, measured at
        sub-second precision on the database clock (the exact inverse of
        consume()'s liveness).
>>>>>>> upstream/master

        Returns:
            Number of rows deleted.
        """
<<<<<<< HEAD
        # Use Unix epoch seconds for reliable comparison (SQLite strftime
        # cannot parse ISO timestamps with '+00:00' timezone offsets).
        now_epoch = int(datetime.now(timezone.utc).timestamp())
=======
>>>>>>> upstream/master

        def operation(conn: Any) -> int:
            # conn is the raw sqlite3 connection DatabaseConnectionManager's
            # generic execute_atomic(Callable[[Any], T]) passes through --
            # matches the identical Any annotation used for this same
            # closure parameter throughout every sibling module.
<<<<<<< HEAD
            # Strip timezone suffix from created_at so strftime can parse it,
            # then compare epoch seconds.
            cursor = conn.execute(
                """
                DELETE FROM payload_cache
                WHERE (? - strftime('%s', REPLACE(REPLACE(created_at, '+00:00', ''), 'Z', '')))
                      >= ttl_seconds
                """,
                (now_epoch,),
=======
            cursor = conn.execute(
                f"DELETE FROM payload_cache WHERE NOT ({_live_predicate(_SQLITE_NOW)})"
>>>>>>> upstream/master
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug(
                "PayloadCacheSqliteBackend: cleaned up %d expired cache entries",
                deleted,
            )
        return deleted

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass
