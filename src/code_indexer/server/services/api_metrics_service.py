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
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional
from code_indexer.server.logging_utils import format_error_log, get_log_extra

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
        self._insert_count = 0
        self._insert_count_lock = threading.Lock()

    def initialize(self, db_path: str) -> None:
        """Initialize the database connection and create schema.

        Args:
            db_path: Path to SQLite database file

        Raises:
            ValueError: If db_path is None or empty

        Note:
            Can be called multiple times safely (idempotent).
            Creates the api_metrics table and index if they don't exist.
        """
        if not db_path:
            raise ValueError("db_path must be a non-empty string")

        self._db_path = db_path

        # Ensure parent directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Create table and index using context manager for safe resource handling
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            cursor = conn.cursor()

            # Enable WAL mode for better concurrent write handling
            # WAL allows concurrent reads and writes without blocking
            cursor.execute("PRAGMA journal_mode=WAL")

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS api_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_api_metrics_type_timestamp
                ON api_metrics(metric_type, timestamp)
                """
            )

            conn.commit()

        logger.debug(f"ApiMetricsService initialized with database: {db_path}")

    def _cleanup_old(self) -> None:
        """Remove timestamps older than 24 hours from the database.

        Deletes all records with timestamps older than MAX_TIMESTAMP_AGE_SECONDS.
        Called automatically on each insert to keep the database size bounded.
        """
        if not self._db_path:
            return

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=MAX_TIMESTAMP_AGE_SECONDS)
        ).isoformat()

        with sqlite3.connect(self._db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM api_metrics WHERE timestamp < ?",
                (cutoff,),
            )
            conn.commit()

    def _insert_metric(self, metric_type: str) -> None:
        """Insert a metric record into the database.

        Args:
            metric_type: Type of metric ('semantic', 'other_index', 'regex', 'other_api')

        Note:
            If not initialized, logs a warning and returns without inserting.
            Uses retry logic with exponential backoff for database lock errors.
            Gracefully degrades (logs warning, no crash) if all retries fail.
        """
        if not self._db_path:
            logger.warning(format_error_log(
                "APP-GENERAL-047",
                f"ApiMetricsService not initialized, skipping {metric_type} increment"
            ))
            return

        now = datetime.now(timezone.utc).isoformat()

        # Retry logic for database lock errors
        for attempt in range(MAX_RETRIES):
            try:
                with sqlite3.connect(self._db_path, timeout=30.0) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO api_metrics (metric_type, timestamp) VALUES (?, ?)",
                        (metric_type, now),
                    )
                    conn.commit()
                break  # Success
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.01)
                    time.sleep(delay)
                    continue
                logger.warning(format_error_log(
                    "APP-GENERAL-048",
                    f"Failed to insert metric {metric_type}: {e}"
                ))
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

    def get_metrics(self, window_seconds: int = 60) -> Dict[str, int]:
        """Get metrics for the specified time window.

        Args:
            window_seconds: Time window in seconds. Default is 60 (1 minute).
                Common values: 60 (1 min), 900 (15 min), 3600 (1 hour), 86400 (24 hours)

        Returns:
            Dictionary with counts for each metric category within the window.
        """
        # Return zeros if not initialized
        if not self._db_path:
            return {
                "semantic_searches": 0,
                "other_index_searches": 0,
                "regex_searches": 0,
                "other_api_calls": 0,
            }

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()

        # Query counts grouped by metric type within the window
        with sqlite3.connect(self._db_path, timeout=30.0) as conn:
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
        if not self._db_path:
            return

        with sqlite3.connect(self._db_path, timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM api_metrics")
            conn.commit()


# Global service instance
api_metrics_service = ApiMetricsService()
