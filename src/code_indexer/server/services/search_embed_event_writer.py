"""SearchEmbedEventWriter — durable query-embedding decision events (Story #1293).

Epic #1288 / Story #1293 (Query-Embedding Decision Event Recording). Writes
SearchEmbedEventRecord entries to the search_embed_event table via a
background writer thread that batches inserts for performance — modeled after
SearchEventLogWriter (Story #1159) and ApiMetricsService (Story #1083).

This is the phantom-free, durable source of truth for every query-embedding
decision on every server query path: exactly one row per NEEDED embed, never
a null correlation_id, never a phantom hit.
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass
from queue import Empty, Full, Queue
from typing import Any, List, Optional

from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Maximum batch size per drain cycle (anti-unbounded-loop, Messi #14).
_MAX_DRAIN_BATCH = 500


# ---------------------------------------------------------------------------
# Domain object
# ---------------------------------------------------------------------------


@dataclass
class SearchEmbedEventRecord:
    """One row in the search_embed_event table.

    correlation_id is REQUIRED (never None) — callers (emit_embed_event) must
    supply a UUID fallback when no request-scoped correlation id exists so no
    event is ever written with a null correlation_id (Story #1293 AC-B1/B2).
    """

    timestamp: float
    correlation_id: str
    node_id: str
    provider: str
    model: Optional[str]
    config_digest: Optional[str]
    cache_mode: Optional[str]
    outcome: str
    role: str
    live_batch_id: Optional[str]
    embed_key: Optional[str]
    long_key: Optional[bool]
    latency_ms: Optional[int]
    shadow_cosine: Optional[float]
    audit_sampled: Optional[bool] = None
    audit_cosine: Optional[float] = None


_INSERT_COLUMNS = (
    "timestamp, correlation_id, node_id, provider, model, config_digest, "
    "cache_mode, outcome, role, live_batch_id, embed_key, long_key, "
    "latency_ms, shadow_cosine, audit_sampled, audit_cosine"
)


def _record_to_row(r: SearchEmbedEventRecord) -> tuple:
    return (
        r.timestamp,
        r.correlation_id,
        r.node_id,
        r.provider,
        r.model,
        r.config_digest,
        r.cache_mode,
        r.outcome,
        r.role,
        r.live_batch_id,
        r.embed_key,
        r.long_key,
        r.latency_ms,
        r.shadow_cosine,
        r.audit_sampled,
        r.audit_cosine,
    )


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class SearchEmbedEventSqliteBackend:
    """SQLite backend for search_embed_event table (solo / development mode)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the search_embed_event table and indexes if absent.

        Mirrors migration 032's PostgreSQL DDL (additive-only, backward
        compatible per CLAUDE.md rolling-upgrade safety rules).
        """
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_embed_event (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        correlation_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT,
                        config_digest TEXT,
                        cache_mode TEXT,
                        outcome TEXT NOT NULL,
                        role TEXT NOT NULL,
                        live_batch_id TEXT,
                        embed_key TEXT,
                        long_key INTEGER,
                        latency_ms INTEGER,
                        shadow_cosine REAL,
                        audit_sampled INTEGER,
                        audit_cosine REAL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_see_timestamp "
                    "ON search_embed_event (timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_see_correlation_id "
                    "ON search_embed_event (correlation_id)"
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventSqliteBackend: schema setup failed: %s", exc
            )

    def insert_batch(self, records: List[SearchEmbedEventRecord]) -> None:
        """Insert a batch of records in ONE transaction. No-op for empty batch."""
        if not records:
            return
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                conn.executemany(
                    f"INSERT INTO search_embed_event ({_INSERT_COLUMNS}) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [_record_to_row(r) for r in records],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventSqliteBackend: insert_batch failed: %s", exc
            )
            raise

    def query(
        self,
        correlation_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ):
        """Query events, optionally filtered by correlation_id.

        Returns (events: list[dict], total_count: int), ordered by
        timestamp DESC.
        """
        conditions = []
        params: list = []
        if correlation_id is not None:
            conditions.append("correlation_id = ?")
            params.append(correlation_id)
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                count_sql = f"SELECT COUNT(*) FROM search_embed_event {where_clause}"
                total_count = conn.execute(count_sql, params).fetchone()[0]

                data_sql = (
                    f"SELECT * FROM search_embed_event {where_clause} "
                    f"ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                )
                rows = conn.execute(data_sql, params + [limit, offset]).fetchall()
                events = [dict(row) for row in rows]
                return events, total_count
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("SearchEmbedEventSqliteBackend: query failed: %s", exc)
            return [], 0

    def count_provider_embed_calls(self) -> int:
        """Fault-injection invariant aggregate:

            COUNT(DISTINCT live_batch_id)
            + COUNT(*) WHERE role='direct' AND outcome IN ('miss','shadow_miss')

        Fail-open: returns 0 on any error.
        """
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(DISTINCT live_batch_id) FROM search_embed_event
                            WHERE live_batch_id IS NOT NULL)
                        +
                        (SELECT COUNT(*) FROM search_embed_event
                            WHERE role = 'direct' AND outcome IN ('miss', 'shadow_miss'))
                    """
                ).fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventSqliteBackend: count_provider_embed_calls failed: %s",
                exc,
            )
            return 0

    def update_audit_by_key(
        self,
        *,
        correlation_id: str,
        embed_key: str,
        audit_sampled: bool,
        audit_cosine: Optional[float],
    ) -> int:
        """Stamp audit_* columns onto already-inserted row(s) keyed by
        (correlation_id, embed_key).

        Fail-open: never raises. Returns the affected row count; the caller
        is responsible for WARNING on 0 or >1 matches (AC-B0/B4).
        """
        try:
            conn = sqlite3.connect(self._db_path, timeout=30)
            try:
                cur = conn.execute(
                    "UPDATE search_embed_event SET audit_sampled = ?, audit_cosine = ? "
                    "WHERE correlation_id = ? AND embed_key = ?",
                    (int(audit_sampled), audit_cosine, correlation_id, embed_key),
                )
                conn.commit()
                affected = cur.rowcount
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventSqliteBackend: update_audit_by_key failed: %s", exc
            )
            return 0

        if affected != 1:
            logger.warning(
                "SearchEmbedEventSqliteBackend: update_audit_by_key matched %d rows "
                "(expected 1) for correlation_id=%s embed_key=%s",
                affected,
                correlation_id,
                embed_key,
            )
        return affected


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------


class SearchEmbedEventPostgresBackend:
    """PostgreSQL backend for search_embed_event table (cluster mode)."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_embed_event (
                        id              BIGSERIAL PRIMARY KEY,
                        timestamp       DOUBLE PRECISION NOT NULL,
                        correlation_id  TEXT NOT NULL,
                        node_id         TEXT NOT NULL,
                        provider        TEXT NOT NULL,
                        model           TEXT,
                        config_digest   TEXT,
                        cache_mode      TEXT,
                        outcome         TEXT NOT NULL,
                        role            TEXT NOT NULL,
                        live_batch_id   TEXT,
                        embed_key       TEXT,
                        long_key        BOOLEAN,
                        latency_ms      INTEGER,
                        shadow_cosine   DOUBLE PRECISION,
                        audit_sampled   BOOLEAN,
                        audit_cosine    DOUBLE PRECISION
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_see_timestamp "
                    "ON search_embed_event (timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_see_correlation_id "
                    "ON search_embed_event (correlation_id)"
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventPostgresBackend: schema setup failed: %s", exc
            )

    def insert_batch(self, records: List[SearchEmbedEventRecord]) -> None:
        """Insert a batch of records in ONE transaction/commit (Bug #1181 pattern).

        SET LOCAL synchronous_commit = off relaxes WAL fsync for these
        ephemeral, append-only analytics rows — the commit is still visible
        immediately; only crash durability is relaxed.

        psycopg v3: executemany lives on the CURSOR, not the connection
        (see .claude-memory/feedback_faithful_db_mocks.md) — using the cursor
        keeps this in the SAME transaction as the SET LOCAL above and the
        single commit() below.
        """
        if not records:
            return
        rows = [_record_to_row(r) for r in records]
        placeholders = ", ".join(["%s"] * 16)
        try:
            with self._pool.connection() as conn:
                conn.execute("SET LOCAL synchronous_commit = off")
                with conn.cursor() as cur:
                    cur.executemany(
                        f"INSERT INTO search_embed_event ({_INSERT_COLUMNS}) "
                        f"VALUES ({placeholders})",
                        rows,
                    )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventPostgresBackend: insert_batch failed: %s", exc
            )
            raise

    def query(
        self,
        correlation_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ):
        conditions = []
        params: list = []
        if correlation_id is not None:
            conditions.append("correlation_id = %s")
            params.append(correlation_id)
        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        columns = [
            "id",
            "timestamp",
            "correlation_id",
            "node_id",
            "provider",
            "model",
            "config_digest",
            "cache_mode",
            "outcome",
            "role",
            "live_batch_id",
            "embed_key",
            "long_key",
            "latency_ms",
            "shadow_cosine",
            "audit_sampled",
            "audit_cosine",
        ]
        try:
            with self._pool.connection() as conn:
                count_sql = f"SELECT COUNT(*) FROM search_embed_event {where_clause}"
                total_count = conn.execute(count_sql, params).fetchone()[0]

                data_sql = (
                    f"SELECT * FROM search_embed_event {where_clause} "
                    f"ORDER BY timestamp DESC LIMIT %s OFFSET %s"
                )
                rows = conn.execute(data_sql, params + [limit, offset]).fetchall()
                events = [dict(zip(columns, row)) for row in rows]
                return events, total_count
        except Exception as exc:
            logger.warning("SearchEmbedEventPostgresBackend: query failed: %s", exc)
            return [], 0

    def count_provider_embed_calls(self) -> int:
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(DISTINCT live_batch_id) FROM search_embed_event
                            WHERE live_batch_id IS NOT NULL)
                        +
                        (SELECT COUNT(*) FROM search_embed_event
                            WHERE role = 'direct' AND outcome IN ('miss', 'shadow_miss'))
                    """
                ).fetchone()
                return int(row[0] or 0)
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventPostgresBackend: count_provider_embed_calls failed: %s",
                exc,
            )
            return 0

    def update_audit_by_key(
        self,
        *,
        correlation_id: str,
        embed_key: str,
        audit_sampled: bool,
        audit_cosine: Optional[float],
    ) -> int:
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE search_embed_event SET audit_sampled = %s, "
                        "audit_cosine = %s WHERE correlation_id = %s AND embed_key = %s",
                        (audit_sampled, audit_cosine, correlation_id, embed_key),
                    )
                    affected = cur.rowcount
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SearchEmbedEventPostgresBackend: update_audit_by_key failed: %s", exc
            )
            return 0

        if affected != 1:
            logger.warning(
                "SearchEmbedEventPostgresBackend: update_audit_by_key matched %d rows "
                "(expected 1) for correlation_id=%s embed_key=%s",
                affected,
                correlation_id,
                embed_key,
            )
        return int(affected)


# ---------------------------------------------------------------------------
# Writer service
# ---------------------------------------------------------------------------


class SearchEmbedEventWriter:
    """Background writer for search_embed_event records.

    Hot path (enqueue) NEVER blocks and NEVER raises (Messi #2 anti-fallback,
    Messi #13 anti-silent-failure). Mirrors SearchEventLogWriter's pattern.
    """

    def __init__(self, backend: Any, maxsize: int = 5000) -> None:
        self._backend = backend
        self._queue: Queue = Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._overflow_warned = False

    @property
    def backend(self) -> Any:
        """Public read-only accessor for the underlying storage backend."""
        return self._backend

    def enqueue(self, record: SearchEmbedEventRecord) -> None:
        """Hot path — NEVER blocks, NEVER raises."""
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait(record)
            self._overflow_warned = False
        except Full:
            if not self._overflow_warned:
                logger.warning("search_embed_event queue full — dropping newest events")
                self._overflow_warned = True

    def _drain(self) -> None:
        """Drain at most _MAX_DRAIN_BATCH records and write them to the backend."""
        batch: List[SearchEmbedEventRecord] = []
        for _ in range(_MAX_DRAIN_BATCH):
            try:
                record = self._queue.get_nowait()
                batch.append(record)
            except Empty:
                break
        if batch:
            self._backend.insert_batch(batch)

    def _loop(self) -> None:
        """Background loop: drain every 5 seconds, stop on signal."""
        while not self._stop.wait(timeout=5.0):
            try:
                self._drain()
            except Exception as exc:
                logger.warning("search_embed_event writer error: %s", exc)
        # Final drain on shutdown so no queued records are lost.
        while not self._queue.empty():
            try:
                self._drain()
            except Exception as exc:
                logger.warning("search_embed_event final drain error: %s", exc)

    def start(self) -> None:
        """Start the background writer thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="search-embed-event-writer",
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the background thread to stop and wait for it to drain."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "search_embed_event writer thread did not stop within %.1fs timeout",
                    timeout,
                )

    def flush(self) -> None:
        """Synchronously drain the entire queue NOW (test/manual-flush helper)."""
        while not self._queue.empty():
            self._drain()
