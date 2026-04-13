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
import queue
import random
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
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

# Background writer queue capacity
_QUEUE_MAXSIZE = 10_000

# Bucket write cleanup interval (cleanup every N writes)
_BUCKET_CLEANUP_INTERVAL = 100

# Background writer queue poll timeout (seconds)
_QUEUE_POLL_TIMEOUT_S = 1.0


def _truncate_min1(dt: datetime) -> str:
    """Truncate datetime to 1-minute bucket boundary (zero seconds + microseconds)."""
    return dt.replace(second=0, microsecond=0).isoformat()


def _truncate_min5(dt: datetime) -> str:
    """Truncate datetime to 5-minute bucket boundary."""
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0).isoformat()


def _truncate_hour1(dt: datetime) -> str:
    """Truncate datetime to 1-hour bucket boundary (zero minutes, seconds, microseconds)."""
    return dt.replace(minute=0, second=0, microsecond=0).isoformat()


def _truncate_day1(dt: datetime) -> str:
    """Truncate datetime to 1-day bucket boundary (zero time component)."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


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
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._writer_thread: Optional[threading.Thread] = None
        self._stop_event: threading.Event = threading.Event()

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
            self._writer_thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="api-metrics-writer"
            )
            self._writer_thread.start()
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

    def _writer_loop(self) -> None:
        """Background thread: drain queue and write bucket UPSERTs for all 4 tiers.

        Runs until _stop_event is set. Polls the queue with a bounded timeout so
        the loop terminates cleanly when the service shuts down.
        """
        write_count = 0
        while not self._stop_event.is_set():
            try:
                metric_type, username, timestamp = self._queue.get(
                    timeout=_QUEUE_POLL_TIMEOUT_S
                )
            except queue.Empty:
                continue

            if self._backend is not None:
                try:
                    self._backend.upsert_bucket(
                        username, "min1", _truncate_min1(timestamp), metric_type
                    )
                    self._backend.upsert_bucket(
                        username, "min5", _truncate_min5(timestamp), metric_type
                    )
                    self._backend.upsert_bucket(
                        username, "hour1", _truncate_hour1(timestamp), metric_type
                    )
                    self._backend.upsert_bucket(
                        username, "day1", _truncate_day1(timestamp), metric_type
                    )
                except Exception as e:
                    logger.warning(
                        format_error_log(
                            "APP-GENERAL-050",
                            f"Failed to upsert bucket for {metric_type}/{username}: {e}",
                        )
                    )

                write_count += 1
                if write_count >= _BUCKET_CLEANUP_INTERVAL:
                    try:
                        self._backend.cleanup_expired_buckets()
                    except Exception as e:
                        logger.warning(
                            format_error_log(
                                "APP-GENERAL-051",
                                f"Failed to cleanup expired buckets: {e}",
                            )
                        )
                    write_count = 0

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

    def _insert_metric(self, metric_type: str, username: str = "_anonymous") -> None:
        """Insert a metric record into the database.

        Args:
            metric_type: Type of metric ('semantic', 'other_index', 'regex', 'other_api')
            username: Username for bucket attribution. Defaults to '_anonymous'.

        Note:
            When a storage backend is set, enqueues to the background writer (non-blocking).
            If the queue is full, the metric is dropped with a warning (never crashes).
            Falls back to direct SQLite writes when no backend is configured.
        """
        now = datetime.now(timezone.utc)

        # Story #672: Backend path — enqueue to background writer (non-blocking hot path)
        if self._backend is not None:
            try:
                self._queue.put_nowait((metric_type, username, now))
            except queue.Full:
                logger.warning(
                    format_error_log(
                        "APP-GENERAL-048",
                        f"API metrics queue full, dropping metric {metric_type} for {username}",
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
                now_iso = now.isoformat()

                def _do_insert(conn: sqlite3.Connection) -> None:
                    conn.cursor().execute(
                        "INSERT INTO api_metrics (metric_type, timestamp, node_id) VALUES (?, ?, ?)",
                        (metric_type, now_iso, node_id),
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

    def increment_semantic_search(self, username: str = "_anonymous") -> None:
        """Record a semantic search call timestamp."""
        self._insert_metric("semantic", username=username)

    def increment_other_index_search(self, username: str = "_anonymous") -> None:
        """Record an other index search call timestamp (FTS, temporal, hybrid)."""
        self._insert_metric("other_index", username=username)

    def increment_regex_search(self, username: str = "_anonymous") -> None:
        """Record a regex search call timestamp."""
        self._insert_metric("regex", username=username)

    def increment_other_api_call(self, username: str = "_anonymous") -> None:
        """Record an other API call timestamp."""
        self._insert_metric("other_api", username=username)

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

    def get_metrics_bucketed(
        self,
        period_seconds: int,
        username: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric totals from api_metrics_buckets for the given period.

        Delegates to the backend when available; returns zero counts otherwise.
        cast() is used because _backend is Optional[Any] — importing the
        ApiMetricsBackend protocol here would create a circular dependency since
        storage-layer tests consume this service.

        Args:
            period_seconds: Duration in seconds. Must be in PERIOD_TO_TIER.
            username: When provided, filter to this user's rows only.
                      When None, aggregate across all users.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls.
        """
        if self._backend is not None:
            return cast(
                Dict[str, int],
                self._backend.get_metrics_bucketed(period_seconds, username),
            )
        return {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "other_api_calls": 0,
        }

    def get_metrics_by_user(self, period_seconds: int) -> Dict[str, Dict[str, int]]:
        """Return per-user metric totals from api_metrics_buckets for the given period.

        Delegates to the backend when available; returns empty dict otherwise.
        cast() is used because _backend is Optional[Any] — see get_metrics_bucketed.

        Args:
            period_seconds: Duration in seconds. Must be in PERIOD_TO_TIER.

        Returns:
            Dict mapping username to {metric_type: count}.
        """
        if self._backend is not None:
            return cast(
                Dict[str, Dict[str, int]],
                self._backend.get_metrics_by_user(period_seconds),
            )
        return {}

    def get_metrics_timeseries(self, period_seconds: int) -> List[Tuple[str, str, int]]:
        """Return timeseries data from api_metrics_buckets for the given period.

        Delegates to the backend when available; returns empty list otherwise.
        cast() is used because _backend is Optional[Any] — see get_metrics_bucketed.

        Args:
            period_seconds: Duration in seconds. Must be in PERIOD_TO_TIER.

        Returns:
            List of (bucket_start, metric_type, count) tuples ordered by bucket_start ASC.
        """
        if self._backend is not None:
            return cast(
                List[Tuple[str, str, int]],
                self._backend.get_metrics_timeseries(period_seconds),
            )
        return []

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
