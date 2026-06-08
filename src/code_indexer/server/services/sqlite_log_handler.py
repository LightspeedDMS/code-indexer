"""
SQLiteLogHandler - logging.Handler that writes to SQLite database.

Implements AC5: SQLite Log Storage Infrastructure
- Creates logs table with required schema
- Creates required indexes for efficient queries
- Supports thread-safe concurrent writes
- Stores extra fields: correlation_id, user_id, request_path
- Stores arbitrary extra data as JSON

Bug #1078 fix: emit() no longer performs synchronous DB I/O while holding the
Python logging handler lock.  Instead it extracts the record fields (fast,
CPU-only) and enqueues them.  A dedicated daemon writer thread drains the
queue and performs the actual insert.  This eliminates the lock-contention
bottleneck observed under concurrent server load (32 query threads parked on
logging/__init__.py:901 acquire while the holder was inside emit ->
execute_atomic -> get_connection).
"""

import json
import logging
import queue
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

# Maximum number of pending log records to buffer.  Records beyond this are
# dropped with a counter increment (anti-unbounded-memory — Messi Rule #14).
_QUEUE_MAXSIZE = 10_000

# How long the writer loop polls before checking the stop event (seconds).
_QUEUE_POLL_TIMEOUT_S = 0.5

# How long close() waits for the writer thread to finish draining (seconds).
_CLOSE_DRAIN_TIMEOUT_S = 5.0

# How long to block (bounded) on queue.Full for ERROR/CRITICAL records before
# giving up and incrementing the dropped counter.
_HIGH_SEVERITY_QUEUE_TIMEOUT_S = 2.0

# Minimum seconds between throttled stderr drop-warnings (anti-spam).
_STDERR_THROTTLE_S = 10.0

# Type alias for the items we put on the queue.
_LogItem = Tuple[
    str,  # timestamp
    str,  # level
    str,  # source
    str,  # message
    Optional[str],  # correlation_id
    Optional[str],  # user_id
    Optional[str],  # request_path
    Optional[str],  # extra_data (JSON string or None)
    Optional[str],  # alias
]


class SQLiteLogHandler(logging.Handler):
    """
    Logging handler that writes log records to SQLite database.

    Database Schema (AC5):
        - id: INTEGER PRIMARY KEY AUTOINCREMENT
        - timestamp: TEXT (ISO 8601 format)
        - level: TEXT (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        - source: TEXT (logger name)
        - message: TEXT (formatted log message)
        - correlation_id: TEXT (optional, from extra)
        - user_id: TEXT (optional, from extra)
        - request_path: TEXT (optional, from extra)
        - extra_data: TEXT (JSON, arbitrary extra fields)
        - created_at: TEXT (ISO 8601 timestamp when record created)

    Indexes (AC5):
        - idx_logs_timestamp
        - idx_logs_level
        - idx_logs_correlation_id
        - idx_logs_source

    Thread Safety:
        emit() only enqueues; all DB writes happen on the dedicated writer
        thread (_writer_loop).  The logging handler lock is released before
        any I/O, eliminating the Bug #1078 lock-contention bottleneck.
    """

    def __init__(self, db_path: Path, logs_backend: Optional[Any] = None):
        """
        Initialize SQLiteLogHandler.

        Args:
            db_path: Path to SQLite database file (e.g., ~/.cidx-server/logs.db)
            logs_backend: Optional LogsBackend for delegated writes (Story #526).
                When provided, SQLite initialization is skipped entirely and all
                emit() calls delegate to this backend from the start.
        """
        super().__init__()
        # Per-instance thread-local re-entry guard (Bug #731 secondary defence).
        # Instance-owned so that multiple SQLiteLogHandler instances installed at
        # the root logger do not share guard state and accidentally suppress each
        # other's recursive emit() calls.  self._emit_guard.active is True while
        # this thread is already inside this handler's emit().
        self._emit_guard = threading.local()
        self.db_path = Path(db_path)

        # Optional LogsBackend for delegated writes (Story #500 AC4, Story #526).
        # When set at construction time, SQLite init is skipped (AC2).
        # When set via set_logs_backend(), emit() switches to backend path.
        self._logs_backend: Optional[Any] = logs_backend

        # Optional cluster node identifier (Story #501 AC3).
        # When set, log records are tagged with this node_id so the admin UI
        # can aggregate and filter logs per node in cluster deployments.
        self._node_id: Optional[str] = None

        # Bug #1078: async writer queue and state.
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._stop_event: threading.Event = threading.Event()
        self._dropped: int = 0  # records dropped due to queue.Full
        self._last_stderr_warn: float = 0.0
        self._writer_thread: Optional[threading.Thread] = None

        if logs_backend is not None:
            # AC2: Skip SQLite init entirely when backend provided at construction.
            # No local database file is created in PG mode.
            self._start_writer()
            return

        # Create database and schema on initialization
        self._init_database()
        self._start_writer()

    def _start_writer(self) -> None:
        """Start the single daemon writer thread (Bug #1078).

        Single source of truth for writer-thread construction so the two
        __init__ branches (backend-injected vs direct-SQLite) don't duplicate it.
        """
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="sqlite-log-writer",
        )
        self._writer_thread.start()

    @property
    def dropped_count(self) -> int:
        """Number of log records dropped due to a saturated writer queue."""
        return self._dropped

    def _record_drop(self) -> None:
        """Count a dropped record and surface it via a throttled stderr warning.

        Uses stderr rather than logging because we are inside a logging handler;
        logging here would recurse. Throttled to avoid flooding stderr under
        sustained saturation (Bug #1078 — drops must never be invisible).
        """
        self._dropped += 1
        now = time.monotonic()
        if now - self._last_stderr_warn >= _STDERR_THROTTLE_S:
            self._last_stderr_warn = now
            sys.stderr.write(
                f"[SQLiteLogHandler] dropped {self._dropped} log record(s) "
                "(writer queue saturated)\n"
            )

    def set_logs_backend(self, backend: Any) -> None:
        """Inject LogsBackend for delegated writes (Story #500 AC4).

        After injection, emit() routes through the backend instead of writing
        directly to the local SQLite file.  The direct-SQLite path remains
        intact when no backend is set, preserving backwards compatibility.

        Deprecated: Pass logs_backend to the constructor instead (Story #526).

        Args:
            backend: A LogsBackend-conforming object (SQLite or PostgreSQL).
        """
        import warnings

        warnings.warn(
            "set_logs_backend() is deprecated. Pass logs_backend to constructor instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._logs_backend = backend

    def set_node_id(self, node_id: str) -> None:
        """Set the cluster node identifier for log record tagging (Story #501 AC3).

        After this is called, all log records emitted by this handler will
        include the given node_id so the admin UI can aggregate and filter
        logs by cluster node.

        Args:
            node_id: Unique identifier for this cluster node (e.g. "node-1").
        """
        self._node_id = node_id

    def _init_database(self) -> None:
        """Create database file, logs table, and indexes if they don't exist."""
        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        def _do_init(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()

            # Create logs table with schema from AC5 (Story #876 Phase C adds
            # the `alias` column for lifecycle-runner row tagging).
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    correlation_id TEXT,
                    user_id TEXT,
                    request_path TEXT,
                    extra_data TEXT,
                    alias TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )

            # Migrate pre-existing databases in-place: add alias if missing.
            existing_columns = {
                row[1] for row in cursor.execute("PRAGMA table_info(logs)").fetchall()
            }
            if "alias" not in existing_columns:
                cursor.execute("ALTER TABLE logs ADD COLUMN alias TEXT")

            # Create indexes from AC5 + new alias index (Story #876 Phase C).
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_correlation_id ON logs(correlation_id)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_source ON logs(source)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_alias ON logs(alias)")

        DatabaseConnectionManager.get_instance(str(self.db_path)).execute_atomic(
            _do_init
        )

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get thread-local database connection via DatabaseConnectionManager.

        Delegates to the shared DatabaseConnectionManager singleton which
        handles thread-local caching, stale connection cleanup, and
        proper connection lifecycle management.
        """
        conn: sqlite3.Connection = DatabaseConnectionManager.get_instance(
            str(self.db_path)
        ).get_connection()
        return conn

    def emit(self, record: logging.LogRecord) -> None:
        """
        Enqueue a log record for writing by the background writer thread.

        Bug #1078 fix: this method does ONLY fast, CPU-only work under the
        logging handler lock.  No DB I/O, no get_connection, no execute_atomic.
        The actual INSERT is performed by _writer_loop on the writer thread.

        Re-entry guard (Bug #731): if this thread is already inside emit(),
        silently drop the recursive call to prevent deadlocks.

        Args:
            record: LogRecord instance to write to database
        """
        if getattr(self._emit_guard, "active", False):
            return
        self._emit_guard.active = True
        try:
            # Format the message
            message = self.format(record)

            # Extract timestamp (ISO 8601 format)
            timestamp = datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat()

            # Extract level name
            level = record.levelname

            # Extract source (logger name)
            source = record.name

            # Extract extra fields if present
            correlation_id = getattr(record, "correlation_id", None)
            user_id = getattr(record, "user_id", None)
            request_path = getattr(record, "request_path", None)
            # Story #876 Phase C: repo alias tag for lifecycle-runner ERROR rows.
            alias = getattr(record, "alias", None)

            # Extract additional extra data (exclude known fields)
            known_fields = {
                "correlation_id",
                "user_id",
                "request_path",
                "alias",
                # Standard LogRecord attributes
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "thread",
                "threadName",
                "exc_info",
                "exc_text",
                "stack_info",
            }

            extra_data: Dict[str, Any] = {}
            for key, value in record.__dict__.items():
                if key not in known_fields:
                    extra_data[key] = value

            # Remove dedicated-column fields from extra_data defensively —
            # they must never leak back into the JSON blob (Story #876 Phase C
            # adds `alias` to this list).
            extra_data.pop("correlation_id", None)
            extra_data.pop("user_id", None)
            extra_data.pop("request_path", None)
            extra_data.pop("alias", None)

            # Serialize extra data as JSON (or NULL if empty)
            extra_data_json: Optional[str] = None
            if extra_data:
                extra_data_json = json.dumps(extra_data)

            item: _LogItem = (
                timestamp,
                level,
                source,
                message,
                correlation_id,
                user_id,
                request_path,
                extra_data_json,
                alias,
            )

            # Enqueue for the writer thread — non-blocking on the common path.
            # On queue.Full: ERROR/CRITICAL records block briefly rather than be
            # lost (they're rare; a short bounded wait cannot recreate the
            # original stall, which was EVERY log doing a synchronous DB write
            # under the handler lock). Lower-severity records drop and are
            # counted + surfaced to stderr (anti-silent-failure, Bug #1078).
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                if record.levelno >= logging.ERROR:
                    try:
                        self._queue.put(item, timeout=_HIGH_SEVERITY_QUEUE_TIMEOUT_S)
                    except queue.Full:
                        self._record_drop()
                else:
                    self._record_drop()

        except Exception:
            # Don't let logging failures crash the application;
            # use handleError to report the issue via stderr (if raiseExceptions).
            self.handleError(record)
        finally:
            self._emit_guard.active = False

    def _writer_loop(self) -> None:
        """Background daemon thread: drain queue and perform actual DB writes.

        Runs until the stop event is set AND the queue is empty.  Uses a
        bounded poll timeout so the thread exits cleanly on shutdown.

        Writer-thread re-entry guard: while this thread is inside insert_log()
        or execute_atomic(), any logging it triggers must not re-enqueue.
        We set _emit_guard.active = True on this thread before each DB write
        so that recursive calls to emit() from within the DB layer are dropped
        rather than re-enqueued (replacing the old cross-thread deadlock with a
        clean drop — Bug #731 / Bug #1078).
        """
        while True:
            try:
                item = self._queue.get(timeout=_QUEUE_POLL_TIMEOUT_S)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue

            (
                timestamp,
                level,
                source,
                message,
                correlation_id,
                user_id,
                request_path,
                extra_data_json,
                alias,
            ) = item

            # Set the re-entry guard for THIS thread while we do DB I/O.
            # Any logging triggered by the DB layer will be dropped cleanly.
            self._emit_guard.active = True
            try:
                if self._logs_backend is not None:
                    # Delegated path (Story #500 AC4): route through injected
                    # LogsBackend.  Supports both SQLite and PostgreSQL backends
                    # transparently.  node_id is injected by set_node_id() in
                    # cluster mode (Story #501 AC3).  alias carries the repo tag
                    # for lifecycle-runner rows (Story #876 Phase C).
                    self._logs_backend.insert_log(
                        timestamp=timestamp,
                        level=level,
                        source=source,
                        message=message,
                        correlation_id=correlation_id,
                        user_id=user_id,
                        request_path=request_path,
                        extra_data=extra_data_json,
                        node_id=self._node_id,
                        alias=alias,
                    )
                else:
                    # Direct-SQLite path (backwards compatible, no backend
                    # injected).  alias is persisted to its own column to stay
                    # consistent with the delegated path (Story #876 Phase C).
                    def _do_insert(conn: sqlite3.Connection) -> None:
                        conn.execute(
                            """
                        INSERT INTO logs (timestamp, level, source, message, correlation_id, user_id, request_path, extra_data, alias)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            (
                                timestamp,
                                level,
                                source,
                                message,
                                correlation_id,
                                user_id,
                                request_path,
                                extra_data_json,
                                alias,
                            ),
                        )

                    DatabaseConnectionManager.get_instance(
                        str(self.db_path)
                    ).execute_atomic(_do_insert)
            except Exception:
                # DB write failures must not crash the writer thread.
                # We can't log here (would recurse), so swallow silently.
                pass
            finally:
                self._emit_guard.active = False

            self._queue.task_done()

    def close(self) -> None:
        """Flush remaining queued records, stop writer thread, then close handler.

        Signals the writer thread to stop, waits for the queue to drain (up to
        _CLOSE_DRAIN_TIMEOUT_S), then joins the thread.  Called by logging
        shutdown; must not hang indefinitely.
        """
        self._stop_event.set()
        if hasattr(self, "_writer_thread") and self._writer_thread is not None:
            self._writer_thread.join(timeout=_CLOSE_DRAIN_TIMEOUT_S)
        super().close()
