"""
API Metrics Service for Story #4 AC2 - Rolling Window Implementation.

Tracks API call timestamps using rolling window approach:
- Semantic Searches (search_code with semantic mode)
- Other Index Searches (FTS, temporal, hybrid searches)
- Regex Searches (regex_search calls)
- All Other API Calls (remaining API endpoints)

SQLite database storage allows multiple uvicorn workers to share metrics.
Timestamps older than 24 hours are automatically cleaned up.
"""

import logging
import random
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

# Maximum age for timestamps - 24 hours
MAX_TIMESTAMP_AGE_SECONDS = 86400  # 24 hours

# Cleanup interval - only cleanup every N inserts (reduces write contention)
CLEANUP_INTERVAL = 100

# Retry configuration for database operations
MAX_RETRIES = 5
RETRY_BASE_DELAY = 0.01  # 10ms base delay


class ApiMetricsService:
    """Service for tracking API call metrics using rolling windows.

    SQLite database storage allows multiple uvicorn workers to share metrics.
    Timestamps are stored per API call category and filtered by window on read.

    Usage:
        service = ApiMetricsService()
        service.initialize("/path/to/metrics.db")
        service.increment_semantic_search()
        metrics = service.get_metrics(window_seconds=60)
    """

    def __init__(self):
        """Initialize the API metrics service (database not yet connected)."""
        self._db_path: Optional[str] = None
        self._conn_manager: Optional[DatabaseConnectionManager] = None
        self._insert_count = 0
        self._insert_count_lock = threading.Lock()
        self._backend: Optional[Any] = None
        self._node_id: Optional[str] = None

    def initialize(
        self,
        db_path: str,
        storage_backend: Optional[Any] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Initialize the database connection and create schema.

        Args:
            db_path: Path to SQLite database file (used only when storage_backend is None)
            storage_backend: Optional injected storage backend (e.g. ApiMetricsSqliteBackend
                or ApiMetricsPostgresBackend). When provided, SQLite setup is skipped.
            node_id: Optional cluster node identifier. Defaults to socket.gethostname()
                when not provided.

        Raises:
            ValueError: If db_path is None or empty and no storage_backend is provided

        Note:
            Can be called multiple times safely (idempotent).
            Creates the api_metrics table and index if they don't exist (SQLite-only mode).
        """
        self._node_id = node_id or socket.gethostname()
        self._backend = storage_backend

        if storage_backend is not None:
            logger.debug(
                f"ApiMetricsService using injected storage backend "
                f"(node_id={self._node_id!r})"
            )
            return

        if not db_path:
            raise ValueError("db_path must be a non-empty string")

        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

        # Ensure parent directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Enable WAL mode outside any transaction — PRAGMA journal_mode=WAL
        # cannot be executed inside a transaction (execute_atomic wraps in BEGIN).
        conn = self._conn_manager.get_connection()
        conn.execute("PRAGMA journal_mode=WAL")

        # Create table and index using atomic operation for safe resource handling
        def _do_schema(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS api_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node_id TEXT
                )
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_api_metrics_type_timestamp
                ON api_metrics(metric_type, timestamp)
                """
            )

            # Migrate existing databases: add node_id column if missing
            cursor.execute("PRAGMA table_info(api_metrics)")
            columns = {row[1] for row in cursor.fetchall()}
            if "node_id" not in columns:
                cursor.execute("ALTER TABLE api_metrics ADD COLUMN node_id TEXT")

        self._conn_manager.execute_atomic(_do_schema)

        logger.debug(f"ApiMetricsService initialized with database: {db_path}")

    def _cleanup_old(self) -> None:
        """Remove timestamps older than 24 hours from the database.

        Deletes all records with timestamps older than MAX_TIMESTAMP_AGE_SECONDS.
        Called automatically on each insert to keep the database size bounded.
        """
        if self._backend is not None:
            return  # Backend handles its own cleanup
        if not self._conn_manager:
            return

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=MAX_TIMESTAMP_AGE_SECONDS)
        ).isoformat()

        def _do_delete(conn: sqlite3.Connection) -> None:
            conn.cursor().execute(
                "DELETE FROM api_metrics WHERE timestamp < ?",
                (cutoff,),
            )

        self._conn_manager.execute_atomic(_do_delete)

    def _insert_metric(self, metric_type: str) -> None:
        """Insert a metric record into the database.

        Args:
            metric_type: Type of metric ('semantic', 'other_index', 'regex', 'other_api')

        Note:
            If not initialized, logs a warning and returns without inserting.
            Uses retry logic with exponential backoff for database lock errors.
            Gracefully degrades (logs warning, no crash) if all retries fail.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Story #531: Backend delegation path (PG or SQLite via registry)
        if self._backend is not None:
            try:
                self._backend.insert_metric(metric_type, now, self._node_id)
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-048",
                        f"Failed to insert metric {metric_type} via backend: {e}",
                    )
                )
            return

        if not self._conn_manager:
            logger.warning(
                format_error_log(
                    "APP-GENERAL-047",
                    f"ApiMetricsService not initialized, skipping {metric_type} increment",
                )
            )
            return

        node_id = self._node_id

        # Retry logic for database lock errors
        for attempt in range(MAX_RETRIES):
            try:

                def _do_insert(conn: sqlite3.Connection) -> None:
                    conn.cursor().execute(
                        "INSERT INTO api_metrics (metric_type, timestamp, node_id) VALUES (?, ?, ?)",
                        (metric_type, now, node_id),
                    )

                self._conn_manager.execute_atomic(_do_insert)
                break  # Success
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 0.01)
                    time.sleep(delay)
                    continue
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-048", f"Failed to insert metric {metric_type}: {e}"
                    )
                )
                return  # Graceful degradation

        # Periodic cleanup (not on every insert)
        with self._insert_count_lock:
            self._insert_count += 1
            should_cleanup = self._insert_count >= CLEANUP_INTERVAL
            if should_cleanup:
                self._insert_count = 0

        if should_cleanup:
            self._cleanup_old()

    def increment_semantic_search(self) -> None:
        """Record a semantic search call timestamp."""
        self._insert_metric("semantic")

    def increment_other_index_search(self) -> None:
        """Record an other index search call timestamp (FTS, temporal, hybrid)."""
        self._insert_metric("other_index")

    def increment_regex_search(self) -> None:
        """Record a regex search call timestamp."""
        self._insert_metric("regex")

    def increment_other_api_call(self) -> None:
        """Record an other API call timestamp."""
        self._insert_metric("other_api")

    def set_node_id(self, node_id: str) -> None:
        """Update the node_id used for metric tagging.

        Called after cluster config resolves the configured node identifier,
        which may differ from the default socket.gethostname().
        """
        self._node_id = node_id

    def get_metrics(
        self, window_seconds: int = 60, node_id: Optional[str] = None
    ) -> Dict[str, int]:
        """Get metrics for the specified time window.

        Args:
            window_seconds: Time window in seconds. Default is 60 (1 minute).
                Common values: 60 (1 min), 900 (15 min), 3600 (1 hour), 86400 (24 hours)
            node_id: When provided, filter to metrics from this node only.
                     When None, aggregate across all nodes.

        Returns:
            Dictionary with counts for each metric category within the window.
        """
        _zeros: Dict[str, int] = {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "other_api_calls": 0,
        }

        # Story #531: Backend delegation path
        if self._backend is not None:
            try:
                return self._backend.get_metrics(window_seconds, node_id=node_id)  # type: ignore[no-any-return]
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-049",
                        f"Failed to get metrics via backend: {e}",
                    )
                )
                return _zeros

        # Return zeros if not initialized
        if not self._conn_manager:
            return _zeros

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()

        # Query counts grouped by metric type within the window
        conn = self._conn_manager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT metric_type, COUNT(*) as count
            FROM api_metrics
            WHERE timestamp >= ?
            GROUP BY metric_type
            """,
            (cutoff,),
        )
        rows = cursor.fetchall()

        # Build result dict from query results
        counts = {row[0]: row[1] for row in rows}

        return {
            "semantic_searches": counts.get("semantic", 0),
            "other_index_searches": counts.get("other_index", 0),
            "regex_searches": counts.get("regex", 0),
            "other_api_calls": counts.get("other_api", 0),
        }

    def reset(self) -> None:
        """Clear all timestamp data from the database.

        Note: With rolling window approach, this method is largely unnecessary
        as timestamps naturally age out. Kept for backward compatibility and testing.
        """
        if self._backend is not None:
            self._backend.reset()
            return

        if not self._conn_manager:
            return

        def _do_reset(conn: sqlite3.Connection) -> None:
            conn.cursor().execute("DELETE FROM api_metrics")

        self._conn_manager.execute_atomic(_do_reset)


# Global service instance
api_metrics_service = ApiMetricsService()
