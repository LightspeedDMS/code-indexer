"""SearchEventLogWriter — per-query search event logging (Issue #1159).

Writes SearchEventRecord entries to the search_event_log table via a
background writer thread that batches inserts for performance.

Modeled after ApiMetricsService (Story #1083) writer/drain pattern.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any, List, Optional

from code_indexer.server.services.config_service import (
    get_config_service as get_config_service_for_eviction,
)
from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Maximum batch size per drain cycle (anti-unbounded-loop, Messi #14).
_MAX_DRAIN_BATCH = 500

# Eviction interval: run prune at most once per 24 hours.
_EVICTION_INTERVAL_SECONDS = 86400.0

# Default retention window when config is unavailable.
DEFAULT_RETENTION_DAYS = 90

# Seconds per day (used for cutoff calculation).
SECONDS_PER_DAY = 86400


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


@dataclass
class SearchEventRecord:
    """One row in the search_event_log table."""

    timestamp: float
    username: str
    repo_alias: Optional[str]
    search_type: str
    query_text: str
    voyage_cache_hit: Optional[bool]
    voyage_cache_mode: Optional[str]
    voyage_latency_ms: Optional[int]
    cohere_cache_hit: Optional[bool]
    cohere_cache_mode: Optional[str]
    cohere_latency_ms: Optional[int]
    total_latency_ms: int
    result_count: int
    node_id: str
    correlation_id: Optional[str]


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class SearchEventLogSqliteBackend:
    """SQLite backend for search_event_log table (solo / development mode)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the search_event_log table and indexes if they do not exist."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_event_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        username TEXT NOT NULL,
                        repo_alias TEXT,
                        search_type TEXT NOT NULL,
                        query_text TEXT NOT NULL,
                        voyage_cache_hit INTEGER,
                        voyage_cache_mode TEXT,
                        voyage_latency_ms INTEGER,
                        cohere_cache_hit INTEGER,
                        cohere_cache_mode TEXT,
                        cohere_latency_ms INTEGER,
                        total_latency_ms INTEGER NOT NULL,
                        result_count INTEGER NOT NULL,
                        node_id TEXT NOT NULL,
                        correlation_id TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_timestamp
                        ON search_event_log (timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_user
                        ON search_event_log (username)
                    """
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("SearchEventLogSqliteBackend: schema setup failed: %s", exc)

    def insert_batch(self, records: List[SearchEventRecord]) -> None:
        """Insert a batch of records. No-op for empty batch."""
        if not records:
            return
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO search_event_log
                        (timestamp, username, repo_alias, search_type, query_text,
                         voyage_cache_hit, voyage_cache_mode, voyage_latency_ms,
                         cohere_cache_hit, cohere_cache_mode, cohere_latency_ms,
                         total_latency_ms, result_count, node_id, correlation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r.timestamp,
                            r.username,
                            r.repo_alias,
                            r.search_type,
                            r.query_text,
                            (
                                int(r.voyage_cache_hit)
                                if r.voyage_cache_hit is not None
                                else None
                            ),
                            r.voyage_cache_mode,
                            r.voyage_latency_ms,
                            (
                                int(r.cohere_cache_hit)
                                if r.cohere_cache_hit is not None
                                else None
                            ),
                            r.cohere_cache_mode,
                            r.cohere_latency_ms,
                            r.total_latency_ms,
                            r.result_count,
                            r.node_id,
                            r.correlation_id,
                        )
                        for r in records
                    ],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("SearchEventLogSqliteBackend: insert_batch failed: %s", exc)
            raise

    def prune_older_than(self, cutoff_timestamp: float) -> None:
        """Delete records older than cutoff_timestamp (Unix epoch seconds)."""
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute(
                    "DELETE FROM search_event_log WHERE timestamp < ?",
                    (cutoff_timestamp,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "SearchEventLogSqliteBackend: prune_older_than failed: %s", exc
            )

    def query(
        self,
        username: Optional[str] = None,
        search_type: Optional[str] = None,
        repo_alias: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
    ):
        """Query events with optional filters.

        Time range is half-open: [from_ts, to_ts).
        Results ordered by timestamp DESC.
        Returns (events: list[dict], total_count: int).
        """
        conditions = []
        params: list = []

        if username is not None:
            conditions.append("username = ?")
            params.append(username)
        if search_type is not None:
            conditions.append("search_type = ?")
            params.append(search_type)
        if repo_alias is not None:
            conditions.append("repo_alias = ?")
            params.append(repo_alias)
        if from_ts is not None:
            conditions.append("timestamp >= ?")
            params.append(from_ts)
        if to_ts is not None:
            conditions.append("timestamp < ?")
            params.append(to_ts)

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                # Count total
                count_sql = f"SELECT COUNT(*) FROM search_event_log {where_clause}"
                total_count = conn.execute(count_sql, params).fetchone()[0]

                # Fetch page
                data_sql = (
                    f"SELECT * FROM search_event_log {where_clause} "
                    f"ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                )
                rows = conn.execute(data_sql, params + [limit, offset]).fetchall()
                events = [dict(row) for row in rows]
                return events, total_count
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("SearchEventLogSqliteBackend: query failed: %s", exc)
            return [], 0


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------


class SearchEventLogPostgresBackend:
    """PostgreSQL backend for search_event_log table (cluster mode)."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the search_event_log table and indexes if they do not exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_event_log (
                        id                  BIGSERIAL PRIMARY KEY,
                        timestamp           DOUBLE PRECISION NOT NULL,
                        username            TEXT NOT NULL,
                        repo_alias          TEXT,
                        search_type         TEXT NOT NULL,
                        query_text          TEXT NOT NULL,
                        voyage_cache_hit    BOOLEAN,
                        voyage_cache_mode   TEXT,
                        voyage_latency_ms   INTEGER,
                        cohere_cache_hit    BOOLEAN,
                        cohere_cache_mode   TEXT,
                        cohere_latency_ms   INTEGER,
                        total_latency_ms    INTEGER NOT NULL,
                        result_count        INTEGER NOT NULL,
                        node_id             TEXT NOT NULL,
                        correlation_id      TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_timestamp
                        ON search_event_log (timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_sel_user
                        ON search_event_log (username)
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEventLogPostgresBackend: schema setup failed: %s", exc
            )

    def insert_batch(self, records: List[SearchEventRecord]) -> None:
        """Insert a batch of records. No-op for empty batch."""
        if not records:
            return
        try:
            with self._pool.connection() as conn:
                for r in records:
                    conn.execute(
                        """
                        INSERT INTO search_event_log
                            (timestamp, username, repo_alias, search_type, query_text,
                             voyage_cache_hit, voyage_cache_mode, voyage_latency_ms,
                             cohere_cache_hit, cohere_cache_mode, cohere_latency_ms,
                             total_latency_ms, result_count, node_id, correlation_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            r.timestamp,
                            r.username,
                            r.repo_alias,
                            r.search_type,
                            r.query_text,
                            r.voyage_cache_hit,
                            r.voyage_cache_mode,
                            r.voyage_latency_ms,
                            r.cohere_cache_hit,
                            r.cohere_cache_mode,
                            r.cohere_latency_ms,
                            r.total_latency_ms,
                            r.result_count,
                            r.node_id,
                            r.correlation_id,
                        ),
                    )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEventLogPostgresBackend: insert_batch failed: %s", exc
            )
            raise

    def prune_older_than(self, cutoff_timestamp: float) -> None:
        """Delete records older than cutoff_timestamp (Unix epoch seconds)."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM search_event_log WHERE timestamp < %s",
                    (cutoff_timestamp,),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEventLogPostgresBackend: prune_older_than failed: %s", exc
            )

    def query(
        self,
        username: Optional[str] = None,
        search_type: Optional[str] = None,
        repo_alias: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
    ):
        """Query events with optional filters.

        Time range is half-open: [from_ts, to_ts).
        Results ordered by timestamp DESC.
        Returns (events: list[dict], total_count: int).
        """
        conditions = []
        params: list = []

        if username is not None:
            conditions.append("username = %s")
            params.append(username)
        if search_type is not None:
            conditions.append("search_type = %s")
            params.append(search_type)
        if repo_alias is not None:
            conditions.append("repo_alias = %s")
            params.append(repo_alias)
        if from_ts is not None:
            conditions.append("timestamp >= %s")
            params.append(from_ts)
        if to_ts is not None:
            conditions.append("timestamp < %s")
            params.append(to_ts)

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        try:
            with self._pool.connection() as conn:
                count_sql = f"SELECT COUNT(*) FROM search_event_log {where_clause}"
                total_count = conn.execute(count_sql, params).fetchone()[0]

                data_sql = (
                    f"SELECT * FROM search_event_log {where_clause} "
                    f"ORDER BY timestamp DESC LIMIT %s OFFSET %s"
                )
                rows = conn.execute(data_sql, params + [limit, offset]).fetchall()
                events = [dict(row) for row in rows]
                return events, total_count
        except Exception as exc:
            logger.warning("SearchEventLogPostgresBackend: query failed: %s", exc)
            return [], 0


# ---------------------------------------------------------------------------
# Writer service
# ---------------------------------------------------------------------------


class SearchEventLogWriter:
    """Background writer for search event records.

    Maintains an in-memory queue and drains it in a background thread,
    writing batches to the storage backend. The hot path (enqueue) NEVER
    blocks and NEVER raises (Messi #2 anti-fallback, Messi #13 anti-silent-failure).
    """

    def __init__(self, backend: Any, maxsize: int = 5000) -> None:
        self._backend = backend
        self._queue: Queue = Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._overflow_warned = False
        # 0.0 so the first _maybe_evict call always fires (Messi #14: provable bound).
        self._last_eviction: float = 0.0

    @property
    def backend(self) -> Any:
        """Public read-only accessor for the underlying storage backend."""
        return self._backend

    def enqueue(self, record: SearchEventRecord) -> None:
        """Hot path — NEVER blocks, NEVER raises.

        Drops the newest event on queue overflow with a one-time WARNING
        (the flag resets after any successful enqueue so the next overflow
        will also warn once).
        """
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait(record)
            self._overflow_warned = False
        except Full:
            if not self._overflow_warned:
                logger.warning("search_event_log queue full — dropping newest events")
                self._overflow_warned = True

    def _drain(self) -> None:
        """Drain at most _MAX_DRAIN_BATCH records and write them to the backend."""
        batch: List[SearchEventRecord] = []
        for _ in range(_MAX_DRAIN_BATCH):
            try:
                record = self._queue.get_nowait()
                batch.append(record)
            except Empty:
                break
        if batch:
            self._backend.insert_batch(batch)

    def _maybe_evict(self) -> None:
        """Run prune_older_than at most once every 24 hours."""
        now = time.time()
        if now - self._last_eviction < _EVICTION_INTERVAL_SECONDS:
            return
        try:
            retention_days = (
                get_config_service_for_eviction()
                .get_config()
                .search_event_log_retention_days
            )
        except Exception as exc:
            logger.warning(
                "search_event_log: could not read retention config (%s); "
                "using default %d days",
                exc,
                DEFAULT_RETENTION_DAYS,
            )
            retention_days = DEFAULT_RETENTION_DAYS
        # Clamp to valid range regardless of source — defends against stale/corrupt config.
        retention_days = max(1, min(3650, retention_days))
        cutoff = now - (retention_days * SECONDS_PER_DAY)
        self._backend.prune_older_than(cutoff)
        self._last_eviction = now

    def _loop(self) -> None:
        """Background loop: drain + evict every 5 seconds, stop on signal."""
        while not self._stop.wait(timeout=5.0):
            try:
                self._drain()
                self._maybe_evict()
            except Exception as exc:
                logger.warning("search_event_log writer error: %s", exc)
        # Final drain on shutdown so no queued records are lost.
        while not self._queue.empty():
            try:
                self._drain()
            except Exception as exc:
                logger.warning("search_event_log final drain error: %s", exc)

    def start(self) -> None:
        """Start the background writer thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="search-event-log-writer",
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the background thread to stop and wait for it to drain."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "search_event_log writer thread did not stop within %.1fs timeout",
                    timeout,
                )
