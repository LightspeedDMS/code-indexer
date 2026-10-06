"""
AuditLogService - Dedicated audit log service for CIDX server.

Story #399: Audit Log Consolidation & AuditLogService Extraction

Owns the audit_logs SQLite table (extracted from GroupAccessManager).
Receives events from:
- GroupAccessManager call sites (group/user/repo admin actions)
- PasswordChangeAuditLogger (auth events)

Provides:
- log()            : Insert an audit event
- query_page() / count_capped() / aggregate() / find_terminal_rows():
                     the storage half of the shared read path
                     (services/audit_log_query.query_audit_log)
- get_pr_logs()    : Query PR creation events (replaces flat-file parse)
- get_cleanup_logs(): Query git cleanup events (replaces flat-file parse)

Also exports:
- migrate_flat_file_to_sqlite(): One-shot startup migration from password_audit.log
"""

import json
import logging
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from code_indexer.server.services.audit_capture import (
    QUEUE_SATURATED,
    WRITE_FAILED,
    WRITER_NOT_RUNNING,
    is_server_process,
    record_legacy,
    report_drop,
    report_unwritten_at_stop,
)
from code_indexer.server.services.audit_events import (
    AUDIT_ROW_COLUMNS,
    AuditEvent,
    build_legacy_event,
    event_row_values,
)
from code_indexer.server.services.audit_log_query import (
    SQLITE_DIALECT,
    AuditFilters,
    build_aggregate_sql,
    build_count_sql,
    build_page_sql,
    build_terminal_rows_sql,
)
from code_indexer.server.services.siem_delivery.capture import (
    SiemDestinations,
    prepare_captures,
    record_transaction_failure,
    write_captures,
)
from code_indexer.server.services.siem_delivery.capture import (
    retried_attempt as siem_retried_attempt,
)
from code_indexer.server.services.siem_delivery.db import (
    SQLITE,
    ensure_sqlite_schema,
)
from code_indexer.server.storage.database_manager import DatabaseConnectionManager

# Issue #1241 P1.3: async-batched audit writer constants.
# Audit durability matters: generous queue cap so saturation is rare.
_AUDIT_QUEUE_MAXSIZE = 50_000
# Max records to coalesce per drain cycle.
_AUDIT_MAX_DRAIN_BATCH = 512
# Poll timeout for the writer loop (seconds).
_AUDIT_POLL_TIMEOUT_S = 0.5
# How long stop() waits for the writer to drain (seconds).
_AUDIT_STOP_TIMEOUT_S = 10.0

logger = logging.getLogger(__name__)

# PR-related action_type values
_PR_ACTION_TYPES = (
    "pr_creation_success",
    "pr_creation_failure",
    "pr_creation_disabled",
)

# Cleanup action_type value
_CLEANUP_ACTION_TYPE = "git_cleanup"

# Public aliases (Issue #1646/#1647): handle_query_audit_logs
# (mcp/handlers/admin/__init__.py) reuses these to recognize PR/cleanup rows
# for field enrichment when merging them out of one general query() call,
# instead of re-declaring a third duplicate of these literals.
PR_ACTION_TYPES = _PR_ACTION_TYPES
CLEANUP_ACTION_TYPE = _CLEANUP_ACTION_TYPE

# Attribution columns added to audit_logs (same names and types as the
# PostgreSQL migration 053_audit_logs_attribution_columns.sql).  Legacy rows
# keep NULL (0 for actor_is_system).
AUDIT_ATTRIBUTION_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("outcome", "TEXT"),
    ("source", "TEXT"),
    ("ip_address", "TEXT"),
    ("correlation_id", "TEXT"),
    ("node_id", "TEXT"),
    ("auth_method", "TEXT"),
    ("actor_is_system", "INTEGER NOT NULL DEFAULT 0"),
    ("event_uuid", "TEXT"),
)

# The user an administrator was impersonating over MCP when the action was
# performed (NULL otherwise).  Same name and type as the PostgreSQL migration
# 063_audit_logs_impersonated_user.sql.
AUDIT_IMPERSONATION_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("impersonated_user", "TEXT"),
)

# (index name, indexed columns) -- same names on both backends.
AUDIT_ATTRIBUTION_INDEXES: Tuple[Tuple[str, str], ...] = (
    ("idx_audit_logs_admin_id", "admin_id"),
    ("idx_audit_logs_target_id", "target_id"),
    ("idx_audit_logs_target_type_timestamp", "target_type, timestamp DESC"),
    ("idx_audit_logs_timestamp_id", "timestamp DESC, id DESC"),
    ("idx_audit_logs_correlation_id", "correlation_id"),
    ("idx_audit_logs_event_uuid", "event_uuid"),
)

# Columns every read returns: the original seven plus the attribution columns.
_SELECT_COLUMNS = ", ".join(
    ("id", "timestamp", "admin_id", "action_type")
    + ("target_type", "target_id", "details")
    + tuple(name for name, _ in AUDIT_ATTRIBUTION_COLUMNS)
    + tuple(name for name, _ in AUDIT_IMPERSONATION_COLUMNS)
)

_SQLITE_INSERT_EVENT_SQL = (
    f"INSERT INTO audit_logs ({', '.join(AUDIT_ROW_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in AUDIT_ROW_COLUMNS)})"
)


def add_audit_column_tolerating_race(
    conn: sqlite3.Connection, name: str, ddl_type: str
) -> None:
    """``ALTER TABLE audit_logs ADD COLUMN``, tolerating a concurrent add.

    Several workers can boot against the same file at once; the loser of
    that race gets "duplicate column name", which means the column now
    exists.  Any other error propagates so startup fails loudly.
    """
    try:
        conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {name} {ddl_type}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _insert_event_rows(conn: sqlite3.Connection, events: Sequence[AuditEvent]) -> None:
    """The ONLY statement that inserts audit rows into SQLite."""
    conn.executemany(
        _SQLITE_INSERT_EVENT_SQL, [event_row_values(event) for event in events]
    )


class _WriterRun:
    """State of one writer thread's run, guarded by the service's lock.

    ``in_flight`` is the batch the writer took from the queue and has not
    finished writing.  ``abandoned`` is set by stop() when the writer did not
    exit in time: stop() has then counted ``in_flight`` as unwritten, so the
    writer must not count those rows again, and must count (not write) any
    batch it takes afterwards.
    """

    def __init__(self) -> None:
        self.in_flight: List[AuditEvent] = []
        self.abandoned = False


class AuditLogService:
    """
    Service owning the audit_logs SQLite table.

    Extracted from GroupAccessManager (Story #399 AC1).
    Shares the same groups.db file — uses CREATE TABLE IF NOT EXISTS so it is
    safe to initialise alongside GroupAccessManager.
    """

    def __init__(self, db_path: Path, storage_backend: Any = None) -> None:
        self._backend = storage_backend

        # Issue #1241 P1.3: async writer state (shared across both modes).
        # _writer_thread is None until start() is called; log() is synchronous
        # when the thread is not running (preserves backward-compat for tests
        # and callers that don't call start()).
        self._queue: queue.Queue = queue.Queue(maxsize=_AUDIT_QUEUE_MAXSIZE)
        self._stop_event: threading.Event = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None
        # Guards the writer lifecycle state below and each run's in-flight
        # batch, so a stop can never interleave with an enqueue or a claim.
        self._state_lock = threading.Lock()
        self._writer_run: Optional[_WriterRun] = None
        # True once start() ran: a writer that is not running afterwards was
        # stopped or died, which is not the same as one never started.
        self._ever_started = False

        if self._backend is not None:
            # PG mode: backend owns its own schema; skip SQLite init
            return
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(str(db_path))
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Lifecycle: start / flush / stop  (Issue #1241 P1.3)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background writer thread (async mode).

        After start() is called, log() and log_raw() enqueue items rather
        than writing synchronously.  Call stop() at shutdown to drain.
        """
        with self._state_lock:
            if self._writer_thread is not None and self._writer_thread.is_alive():
                return  # idempotent
            self._stop_event.clear()
            run = _WriterRun()
            self._writer_run = run
            self._ever_started = True
            self._writer_thread = threading.Thread(
                target=self._writer_loop,
                args=(run,),
                daemon=True,
                name="audit-log-writer",
            )
            self._writer_thread.start()

    def flush(self) -> None:
        """Synchronously drain the writer queue without stopping.

        Blocks until every record enqueued before this call has been
        committed by the writer thread.  No-op when the writer is not
        running (synchronous mode).
        """
        thread = self._writer_thread
        if thread is None or not thread.is_alive():
            return
        self._queue.join()

    def stop(self, timeout: float = _AUDIT_STOP_TIMEOUT_S) -> None:
        """Signal the writer to stop and wait up to *timeout* for it to drain.

        A writer that exits in time has written its whole queue.  A writer
        that does not (the store is locked or stalled) is abandoned: the
        batch it holds and every row still queued are counted as not
        written -- ``records_dropped_since_boot`` plus ONE summary ERROR line
        -- rather than lost silently.  Rows held or left queued by a writer
        thread that ended on its own are counted the same way.

        Counting semantics: rows still queued, and the batch the writer has
        marked in flight, when stop() runs are counted before it returns.
        One window is not: a row the writer has just taken off the queue
        but not yet marked in flight at the moment stop() detaches it.  The
        abandoned writer counts that row itself when it resumes -- possibly
        after stop() has returned, or not at all if the process exits first.
        Over-count is bounded by one batch: if the abandoned writer's
        in-flight write later commits, those rows are both written and
        counted.  Rows the abandoned writer takes afterwards are counted,
        never written.
        """
        # Unpublish the writer first: from here on every enqueue is a counted
        # drop, so nothing can be put after the final drain below.
        with self._state_lock:
            thread = self._writer_thread
            run = self._writer_run
            self._writer_thread = None
            self._writer_run = None
        self._stop_event.set()
        if thread is not None:
            thread.join(timeout=timeout)
        unwritten: List[AuditEvent] = []
        with self._state_lock:
            if run is not None:
                # Non-empty only if the writer is still inside a write (it did
                # not exit in time) or ended in the middle of one.
                run.abandoned = True
                unwritten.extend(run.in_flight)
        unwritten.extend(self._drain_queue())
        if unwritten:
            report_unwritten_at_stop(unwritten)

    def _drain_queue(self) -> List[AuditEvent]:
        """Take every item left in the queue (bounded by its size now)."""
        drained: List[AuditEvent] = []
        for _ in range(self._queue.qsize()):
            try:
                drained.append(self._queue.get_nowait())
            except queue.Empty:
                break
            self._queue.task_done()
        return drained

    # ------------------------------------------------------------------
    # Internal: writer loop and batch write
    # ------------------------------------------------------------------

    def _report_write_failure(
        self, event: AuditEvent, exc: Exception, run: Optional[_WriterRun]
    ) -> None:
        """Count a failed row unless stop() already counted it (abandoned)."""
        if run is not None:
            with self._state_lock:
                if run.abandoned:
                    return
        report_drop(WRITE_FAILED, event, exc)

    def _write_batch(
        self, batch: List[AuditEvent], run: Optional[_WriterRun] = None
    ) -> None:
        """Write a batch of events in ONE transaction via insert_events.

        M3: no failure is swallowed silently -- every lost row is counted
            and logged at ERROR through audit_capture.report_drop (event
            identifiers and the exception CLASS only, never row values).
        M4: when a multi-row batch fails it is retried row-by-row so one
            poison row cannot drop up to 511 valid audit records.
        *run* is the writer run holding the batch (None for a synchronous
        write); a row stop() already counted is not counted twice.
        """
        if not batch:
            return
        try:
            if len(batch) == 1:
                self.insert_events(batch)
            else:
                # retried row by row below: only a row that still fails is
                # a SIEM capture gap
                with siem_retried_attempt():
                    self.insert_events(batch)
            return
        except Exception as exc:
            if len(batch) == 1:
                self._report_write_failure(batch[0], exc, run)
                return
            logger.warning(
                "AuditLogService: batch insert failed (%d rows, %s); "
                "retrying row-by-row",
                len(batch),
                type(exc).__name__,
            )
        for event in batch:
            try:
                self.insert_events([event])
            except Exception as row_exc:
                self._report_write_failure(event, row_exc, run)

    def _writer_loop(self, run: _WriterRun) -> None:
        """Background daemon: drain queue in batches and commit to DB.

        Runs until stop_event is set AND the queue is empty, or until stop()
        abandons *run*.
        """
        while True:
            try:
                first_item = self._queue.get(timeout=_AUDIT_POLL_TIMEOUT_S)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue

            batch = [first_item]
            additional = min(self._queue.qsize(), _AUDIT_MAX_DRAIN_BATCH - 1)
            for _ in range(additional):
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            with self._state_lock:
                abandoned = run.abandoned
                if not abandoned:
                    run.in_flight = batch
            if abandoned:
                # stop() gave up on this writer: count, never write late.
                report_unwritten_at_stop(batch)
            else:
                self._write_batch(batch, run)
                with self._state_lock:
                    run.in_flight = []

            for _ in batch:
                self._queue.task_done()
            if abandoned:
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        return self._conn_manager.get_connection()  # type: ignore[no-any-return]

    def _ensure_schema(self) -> None:
        """Create audit_logs table and indexes if they don't exist."""
        # Issue #1241 P1.2: WAL must be set OUTSIDE any transaction.
        # SQLite silently ignores PRAGMA journal_mode = WAL if issued inside
        # BEGIN ... COMMIT (execute_atomic does BEGIN EXCLUSIVE).
        # Use a short-lived raw connection for this once-per-file pragma.
        # Note: busy_timeout is PER-CONNECTION and is set to 30000 ms by
        # DatabaseConnectionManager.get_connection() on every connection it
        # opens, so we do NOT set it here on this throwaway bootstrap connection.
        _bootstrap_conn = sqlite3.connect(str(self._db_path))
        try:
            _bootstrap_conn.execute("PRAGMA journal_mode = WAL")
            _bootstrap_conn.commit()
        finally:
            _bootstrap_conn.close()

        def _do_schema(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    admin_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    details TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_logs(timestamp DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_action_type
                ON audit_logs(action_type)
                """
            )
            # Additive attribution columns: the table may have been created
            # by an older release or by GroupAccessManager's 7-column DDL.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")}
            for name, ddl_type in (
                AUDIT_ATTRIBUTION_COLUMNS + AUDIT_IMPERSONATION_COLUMNS
            ):
                if name not in existing:
                    add_audit_column_tolerating_race(conn, name, ddl_type)
            for index_name, columns in AUDIT_ATTRIBUTION_INDEXES:
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} ON audit_logs({columns})"
                )
            # SIEM delivery tables live in the SAME file, so the queue insert
            # can join the audit transaction.
            ensure_sqlite_schema(conn)

        self._conn_manager.execute_atomic(_do_schema)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def insert_events(
        self,
        events: Sequence[AuditEvent],
        *,
        siem_destinations: Optional[SiemDestinations] = None,
    ) -> None:
        """Write *events* in ONE transaction on the calling thread.

        This is the single write function: every audit row reaches the
        store through here.  In PostgreSQL mode (and in solo mode, where the
        injected backend is itself an unstarted AuditLogService on the same
        file) it delegates to the backend's ``insert_events``.  Raises on
        failure; callers decide how a failure is counted.

        SIEM capture joins the same transaction: projection runs before it
        starts, and each queue row is a savepointed, fail-open INSERT after
        the audit rows.  *siem_destinations* (explicit destinations of
        SIEM self-report rows) is supplied only by those emitters.
        """
        if not events:
            return
        if self._backend is not None:
            if siem_destinations is None:
                self._backend.insert_events(events)
            else:
                self._backend.insert_events(events, siem_destinations=siem_destinations)
            return
        prepared = prepare_captures(events, siem_destinations)

        def _write(conn: sqlite3.Connection) -> None:
            _insert_event_rows(conn, events)
            write_captures(conn, prepared, SQLITE)

        try:
            self._conn_manager.execute_atomic(_write)
        except Exception as exc:
            record_transaction_failure(prepared, exc)
            raise

    def enqueue_event(self, event: AuditEvent) -> None:
        """Hand *event* to the writer thread (QUEUED delivery).

        O(1) and never performs DB I/O on the caller's thread.  A writer
        that is not running, or a saturated queue, is a counted drop --
        there is no synchronous fallback on this path.  The check and the
        put hold the lifecycle lock, so an event is never put after stop()
        has begun (it would sit in a queue nobody drains).
        """
        with self._state_lock:
            thread = self._writer_thread
            running = thread is not None and thread.is_alive()
            if running:
                try:
                    self._queue.put_nowait(event)
                    return
                except queue.Full:
                    pass
        report_drop(QUEUE_SATURATED if running else WRITER_NOT_RUNNING, event)

    def _deliver_legacy(self, event: AuditEvent) -> None:
        """Legacy log()/log_raw() delivery.

        Started: the action type's catalog delivery through this service
        (``audit_capture.record_legacy``) -- a DURABLE row is committed on
        the caller's thread before the call returns; a QUEUED row goes to
        the writer thread, and a full queue is a counted drop (there is no
        synchronous fallback).  Stopped or dead after a start, in a server
        process: a counted drop -- never a synchronous write, which could
        block the event loop during shutdown.  Never started, or not a
        server process (tests, pre-start callers, standalone CLI): a direct
        synchronous write, as before.
        """
        thread = self._writer_thread
        if thread is not None and thread.is_alive():
            record_legacy(self, event)
        elif self._ever_started and is_server_process():
            report_drop(WRITER_NOT_RUNNING, event)
        else:
            self._write_batch([event])

    def log(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
        *,
        outcome: Optional[str] = None,
    ) -> None:
        """
        Insert one audit log entry.

        The row is built as a legacy event (uuid and ambient attribution)
        and delivered as its action type's catalog entry says once start()
        has been called: DURABLE types are committed before this returns,
        QUEUED types go to the async writer.  Without start(), it writes
        synchronously (tests and pre-start callers).

        Args:
            admin_id:    Actor performing the action (username or 'system').
            action_type: Verb describing what happened.
            target_type: Category of the target ('user', 'group', 'repo', 'auth').
            target_id:   Identifier of the specific target.
            details:     Optional JSON string with extra event data.
            outcome:     Explicit outcome; None records the one the action
                         type's name implies (see ``build_legacy_event``).
        """
        self._deliver_legacy(
            build_legacy_event(
                actor=admin_id,
                action_type=action_type,
                target_type=target_type,
                target_id=target_id,
                details_json=details,
                outcome=outcome,
            )
        )

    def log_raw(
        self,
        timestamp: str,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None:
        """Insert an audit entry with an explicit timestamp (for migration use).

        Delivered like log() (see ``_deliver_legacy``).
        """
        self._deliver_legacy(
            build_legacy_event(
                actor=admin_id,
                action_type=action_type,
                target_type=target_type,
                target_id=target_id,
                details_json=details,
                occurred_at=timestamp,
            )
        )

    # ------------------------------------------------------------------
    # Shared read path (services/audit_log_query.py renders the SQL); the
    # ONE way rows are read for the Web page, MCP and REST.
    # ------------------------------------------------------------------

    def _fetch_dicts(self, sql: str, params: Sequence[Any]) -> List[dict]:
        cursor = self._get_connection().cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(sql, list(params))
        return [dict(row) for row in cursor.fetchall()]

    def query_page(
        self,
        filters: "AuditFilters",
        tier: str,
        *,
        seek: Optional[Tuple[str, int]],
        direction: str,
        limit: int,
        offset: int = 0,
    ) -> List[dict]:
        """One keyset page of rows (see ``audit_log_query.build_page_sql``)."""
        if self._backend is not None:
            return self._backend.query_page(  # type: ignore[no-any-return]
                filters,
                tier,
                seek=seek,
                direction=direction,
                limit=limit,
                offset=offset,
            )
        sql, params = build_page_sql(
            filters,
            tier,
            SQLITE_DIALECT,
            seek=seek,
            direction=direction,
            limit=limit,
            offset=offset,
        )
        return self._fetch_dicts(sql, params)

    def count_capped(self, filters: "AuditFilters", tier: str, *, cap: int) -> int:
        """Matching row count, reading at most ``cap + 1`` rows."""
        if self._backend is not None:
            return int(self._backend.count_capped(filters, tier, cap=cap))
        sql, params = build_count_sql(filters, tier, SQLITE_DIALECT, cap=cap)
        return int(self._fetch_dicts(sql, params)[0]["cnt"])

    def aggregate(
        self, filters: "AuditFilters", tier: str, *, max_groups: int
    ) -> List[dict]:
        """``GROUP BY (action_type, outcome)`` of the matching rows, in SQL."""
        if self._backend is not None:
            return self._backend.aggregate(  # type: ignore[no-any-return]
                filters, tier, max_groups=max_groups
            )
        sql, params = build_aggregate_sql(
            filters, tier, SQLITE_DIALECT, max_groups=max_groups
        )
        return self._fetch_dicts(sql, params)

    def find_terminal_rows(self, correlation_ids: Sequence[str]) -> List[dict]:
        """Terminal rows sharing one of *correlation_ids* (pairing lookup)."""
        if not correlation_ids:
            return []
        if self._backend is not None:
            return self._backend.find_terminal_rows(  # type: ignore[no-any-return]
                correlation_ids
            )
        sql, params = build_terminal_rows_sql(correlation_ids, SQLITE_DIALECT)
        return self._fetch_dicts(sql, params)

    def get_pr_logs(
        self,
        repo_alias: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        Query PR creation audit logs.

        Replaces PasswordChangeAuditLogger._parse_logs_by_prefix("PR_CREATION").

        Args:
            repo_alias: Filter by repository alias stored in target_id.
            limit:      Maximum records to return.
            offset:     Records to skip.

        Returns:
            List of audit log dicts (newest first).
        """
        if self._backend is not None:
            return self._backend.get_pr_logs(  # type: ignore[no-any-return]
                repo_alias=repo_alias,
                limit=limit,
                offset=offset,
            )
        conn = self._get_connection()
        placeholders = ",".join("?" * len(_PR_ACTION_TYPES))
        conditions = [f"action_type IN ({placeholders})"]
        params: List[Any] = list(_PR_ACTION_TYPES)

        if repo_alias:
            conditions.append("target_id = ?")
            params.append(repo_alias)

        where = "WHERE " + " AND ".join(conditions)
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM audit_logs
            {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_cleanup_logs(
        self,
        repo_path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        Query git cleanup audit logs.

        Replaces PasswordChangeAuditLogger._parse_logs_by_prefix("GIT_CLEANUP").

        Args:
            repo_path: Filter by repository path stored in target_id.
            limit:     Maximum records to return.
            offset:    Records to skip.

        Returns:
            List of audit log dicts (newest first).
        """
        if self._backend is not None:
            return self._backend.get_cleanup_logs(  # type: ignore[no-any-return]
                repo_path=repo_path,
                limit=limit,
                offset=offset,
            )
        conn = self._get_connection()
        conditions = ["action_type = ?"]
        params: List[Any] = [_CLEANUP_ACTION_TYPE]

        if repo_path:
            conditions.append("target_id = ?")
            params.append(repo_path)

        where = "WHERE " + " AND ".join(conditions)
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM audit_logs
            {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        return [dict(row) for row in cursor.fetchall()]

    def cleanup_old_logs(self, cutoff_iso: str) -> int:
        """Delete audit log records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of rows deleted.
        """
        if self._backend is not None:
            return self._backend.cleanup_old_logs(cutoff_iso)  # type: ignore[no-any-return]
        total_deleted = 0
        while True:
            batch: List[int] = [0]

            def _do_batch(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "DELETE FROM audit_logs WHERE rowid IN "
                    "(SELECT rowid FROM audit_logs WHERE timestamp < ? LIMIT 1000)",
                    (cutoff_iso,),
                )
                batch[0] = conn.execute("SELECT changes()").fetchone()[0]

            self._conn_manager.execute_atomic(_do_batch)
            if batch[0] == 0:
                break
            total_deleted += batch[0]
        return total_deleted


# ---------------------------------------------------------------------------
# Flat-file migration (AC4)
# ---------------------------------------------------------------------------


def _extract_actor(entry: dict) -> str:
    """Derive admin_id from a migrated flat-file entry."""
    # PR / cleanup events are system-originated
    event_type = entry.get("event_type", "")
    if event_type in _PR_ACTION_TYPES or event_type == _CLEANUP_ACTION_TYPE:
        return "system"
    # Auth events: use username, actor_username, or email as actor
    for key in ("username", "actor_username", "email", "client_id"):
        if entry.get(key):
            return str(entry[key])
    return "system"


def _extract_target_id(entry: dict) -> str:
    """Derive target_id from a migrated flat-file entry."""
    event_type = entry.get("event_type", "")
    if event_type in _PR_ACTION_TYPES:
        return str(entry.get("repo_alias") or entry.get("job_id") or "unknown")
    if event_type == _CLEANUP_ACTION_TYPE:
        return str(entry.get("repo_path") or "unknown")
    # Impersonation: target is the impersonated user
    if event_type in ("impersonation_set", "impersonation_cleared"):
        return str(
            entry.get("target_username") or entry.get("previous_target") or "unknown"
        )
    # Auth events: use username or email
    for key in ("username", "actor_username", "email"):
        if entry.get(key):
            return str(entry[key])
    return "unknown"


def migrate_flat_file_to_sqlite(
    log_file: Path,
    audit_service: AuditLogService,
) -> Tuple[int, int]:
    """
    One-shot migration: parse password_audit.log and insert into audit_logs.

    Idempotent: if the file doesn't exist, returns (0, 0) silently.
    Deletes the file after migration (even if all lines were malformed).

    Log line format: "YYYY-MM-DD HH:MM:SS UTC - LEVEL - PREFIX: {json}"

    Args:
        log_file:      Path to the flat log file.
        audit_service: Destination AuditLogService instance.

    Returns:
        (migrated_count, skipped_count)
    """
    if not log_file.exists():
        return 0, 0

    migrated = 0
    skipped = 0

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Find the first '{' — that's where JSON starts
                brace_pos = line.find("{")
                if brace_pos == -1:
                    skipped += 1
                    continue
                try:
                    entry = json.loads(line[brace_pos:])
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                event_type = entry.get("event_type")
                if not event_type:
                    skipped += 1
                    continue

                actor = _extract_actor(entry)
                target_id = _extract_target_id(entry)
                timestamp = (
                    entry.get("timestamp") or datetime.now(timezone.utc).isoformat()
                )
                details = json.dumps(entry)

                # All PasswordChangeAuditLogger events use target_type="auth" by design.
                # Events are distinguished by action_type, not target_type.
                audit_service.log_raw(
                    timestamp=timestamp,
                    admin_id=actor,
                    action_type=event_type,
                    target_type="auth",
                    target_id=target_id,
                    details=details,
                )
                migrated += 1

    except Exception as e:
        logger.warning(f"migrate_flat_file_to_sqlite: error reading {log_file}: {e}")

    # Always delete the file regardless of parse success
    try:
        log_file.unlink()
    except Exception as e:
        logger.warning(f"migrate_flat_file_to_sqlite: could not delete {log_file}: {e}")

    logger.info(
        f"Migrated {migrated} entries from {log_file.name}, "
        f"skipped {skipped} unparseable lines"
    )
    return migrated, skipped
