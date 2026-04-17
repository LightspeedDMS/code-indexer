"""
SQLiteLogHandler - logging.Handler that writes to SQLite database.

Implements AC5: SQLite Log Storage Infrastructure
- Creates logs table with required schema
- Creates required indexes for efficient queries
- Supports thread-safe concurrent writes
- Stores extra fields: correlation_id, user_id, request_path
- Stores arbitrary extra data as JSON
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


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
        Uses thread-local connections for thread-safe concurrent writes.
        Each thread gets its own database connection.
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

        if logs_backend is not None:
            # AC2: Skip SQLite init entirely when backend provided at construction.
            # No local database file is created in PG mode.
            return

        # Create database and schema on initialization
        self._init_database()

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

            # Create logs table with schema from AC5
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
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )

            # Create indexes from AC5
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_correlation_id ON logs(correlation_id)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_source ON logs(source)")

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
        Emit a log record to the SQLite database.

        Re-entry guard (Bug #731): if this thread is already inside emit(),
        silently drop the recursive call to prevent deadlocks caused by
        DatabaseConnectionManager logging while the root-logger lock is held.

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

            # Extract additional extra data (exclude known fields)
            known_fields = {
                "correlation_id",
                "user_id",
                "request_path",
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

            # Remove correlation_id, user_id, request_path from extra_data
            # (they have dedicated columns)
            extra_data.pop("correlation_id", None)
            extra_data.pop("user_id", None)
            extra_data.pop("request_path", None)

            # Serialize extra data as JSON (or NULL if empty)
            extra_data_json: Optional[str] = None
            if extra_data:
                extra_data_json = json.dumps(extra_data)

            if self._logs_backend is not None:
                # Delegated path (Story #500 AC4): route through injected LogsBackend.
                # Supports both SQLite and PostgreSQL backends transparently.
                # node_id is injected by set_node_id() in cluster mode (Story #501 AC3).
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
                )
            else:
                # Direct-SQLite path (backwards compatible, no backend injected).
                def _do_insert(conn: sqlite3.Connection) -> None:
                    conn.execute(
                        """
                    INSERT INTO logs (timestamp, level, source, message, correlation_id, user_id, request_path, extra_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                        ),
                    )

                DatabaseConnectionManager.get_instance(
                    str(self.db_path)
                ).execute_atomic(_do_insert)

        except Exception:
            # Don't let logging failures crash the application
            # Use handleError to report the issue
            self.handleError(record)
        finally:
            self._emit_guard.active = False

    def close(self) -> None:
        """Close handler. Connections are managed by DatabaseConnectionManager."""
        super().close()
