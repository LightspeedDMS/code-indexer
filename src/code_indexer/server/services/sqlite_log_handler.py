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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, cast

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

    def __init__(self, db_path: Path):
        """
        Initialize SQLiteLogHandler.

        Args:
            db_path: Path to SQLite database file (e.g., ~/.cidx-server/logs.db)
        """
        super().__init__()
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._connections: Dict[int, sqlite3.Connection] = {}
        self._last_cleanup: float = 0.0
        self.CLEANUP_INTERVAL: float = 60.0

        # Create database and schema on initialization
        self._init_database()

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

        DatabaseConnectionManager.get_instance(str(self.db_path)).execute_atomic(_do_init)

    def _cleanup_stale_connections(self) -> None:
        """
        Close and remove connections for threads that are no longer alive.

        Called periodically (throttled by CLEANUP_INTERVAL) to prevent
        unbounded memory and file descriptor growth from thread pool churn.
        """
        with self._lock:
            self._last_cleanup = time.time()
            alive_thread_ids = {t.ident for t in threading.enumerate()}
            stale_ids = [
                tid for tid in self._connections if tid not in alive_thread_ids
            ]
            for tid in stale_ids:
                try:
                    self._connections[tid].close()
                except Exception as e:
                    logger.warning(
                        f"Failed to close stale log handler connection for thread {tid}: {e}"
                    )
                del self._connections[tid]

            if stale_ids:
                logger.info(
                    f"Cleaned up {len(stale_ids)} stale SQLite connections"
                )

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get thread-local database connection.

        Each thread gets its own connection for thread safety.
        Connections are cached in thread-local storage and tracked for cleanup.
        """
        thread_id = threading.get_ident()
        if not hasattr(self._local, "connection"):
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=30.0,  # 30 second timeout for lock conflicts
            )
            self._local.connection = conn
            with self._lock:
                self._connections[thread_id] = conn

        # Piggyback stale connection cleanup (throttled)
        if (time.time() - self._last_cleanup) > self.CLEANUP_INTERVAL:
            self._cleanup_stale_connections()

        return cast(sqlite3.Connection, self._local.connection)

    def emit(self, record: logging.LogRecord) -> None:
        """
        Emit a log record to the SQLite database.

        Args:
            record: LogRecord instance to write to database
        """
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

            # Insert log record into database (thread-safe)
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute(
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

            conn.commit()

        except Exception:
            # Don't let logging failures crash the application
            # Use handleError to report the issue
            self.handleError(record)

    def close(self) -> None:
        """Close ALL tracked database connections and cleanup resources."""
        with self._lock:
            for conn in self._connections.values():
                try:
                    conn.close()
                except Exception as e:
                    logger.warning(f"Failed to close log handler connection: {e}")
            self._connections.clear()

        # Clear thread-local reference
        if hasattr(self._local, "connection"):
            delattr(self._local, "connection")

        super().close()
