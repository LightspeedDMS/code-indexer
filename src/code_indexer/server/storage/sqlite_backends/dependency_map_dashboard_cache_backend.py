"""
SQLite backend for dependency map dashboard cache (Story #684).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class DependencyMapDashboardCacheBackend:
    """
    SQLite backend for dependency map dashboard cache (Story #684).

    Provides table creation, cached row retrieval, and cache-freshness checking.
    A single row (cache_key='default') represents the most recently computed
    job-status result.
    """

    _CACHE_KEY = "default"

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        # db_path is not validated here: this is a pure relocation
        # (issue #1935 Part 1) of the original, already-shipped,
        # already-tested constructor -- callers (StorageFactory) always
        # supply a real path, and adding new validation would be an
        # unauthorized behaviour change for this mechanical move.
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create dependency_map_dashboard_cache table if it does not exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dependency_map_dashboard_cache (
                    cache_key TEXT PRIMARY KEY,
                    result_json TEXT,
                    computed_at TEXT,
                    job_id TEXT,
                    last_failure_message TEXT,
                    last_failure_at TEXT
                )
                """
            )

        self._conn_manager.execute_atomic(_op)

    def get_cached(self) -> Optional[Dict[str, Any]]:
        """
        Return the cached row as a dict or None if no row exists.

        Returns:
            Dict with keys result_json, computed_at, job_id,
            last_failure_message, last_failure_at — or None.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT result_json, computed_at, job_id, last_failure_message, last_failure_at "
            "FROM dependency_map_dashboard_cache WHERE cache_key = ?",
            (self._CACHE_KEY,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "result_json": row[0],
            "computed_at": row[1],
            "job_id": row[2],
            "last_failure_message": row[3],
            "last_failure_at": row[4],
        }

    def is_fresh(self, ttl_seconds: int) -> bool:
        """
        Return True if a cached result exists and is within ttl_seconds.

        Args:
            ttl_seconds: Maximum age in seconds. Must be non-negative.

        Returns:
            True if computed_at is set and within TTL, False otherwise.

        Raises:
            ValueError: If ttl_seconds is negative.
        """
        if ttl_seconds < 0:
            raise ValueError(f"ttl_seconds must be non-negative, got {ttl_seconds!r}")
        cached = self.get_cached()
        if cached is None:
            return False
        computed_at_str = cached.get("computed_at")
        if not computed_at_str:
            return False
        try:
            computed_at = datetime.fromisoformat(computed_at_str)
            if computed_at.tzinfo is None:
                computed_at = computed_at.replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - computed_at).total_seconds()
            return age_seconds <= ttl_seconds
        except (ValueError, TypeError) as exc:
            logger.warning(
                "DependencyMapDashboardCacheBackend.is_fresh: "
                "failed to parse computed_at=%r: %s",
                computed_at_str,
                exc,
            )
            return False

    def set_cached(self, result_json: str, job_id: Optional[str] = None) -> None:
        """
        Upsert the cached result, clearing job_id and all failure fields.

        Args:
            result_json: JSON string of the computed result. Must not be None.
            job_id: Accepted for API compatibility but always stored as NULL.

        Raises:
            ValueError: If result_json is None.
        """
        if result_json is None:
            raise ValueError("result_json must not be None")
        now = datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO dependency_map_dashboard_cache
                    (cache_key, result_json, computed_at, job_id,
                     last_failure_message, last_failure_at)
                VALUES (?, ?, ?, NULL, NULL, NULL)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result_json = excluded.result_json,
                    computed_at = excluded.computed_at,
                    job_id = NULL,
                    last_failure_message = NULL,
                    last_failure_at = NULL
                """,
                (self._CACHE_KEY, result_json, now),
            )

        self._conn_manager.execute_atomic(_op)

    def clear_job_slot(self) -> None:
        """Set job_id to NULL, preserving all other fields."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE dependency_map_dashboard_cache SET job_id = NULL WHERE cache_key = ?",
                (self._CACHE_KEY,),
            )

        self._conn_manager.execute_atomic(_op)

    def mark_job_failed(self, error_message: str) -> None:
        """
        Record a job failure: clear job_id, set failure fields.

        Preserves existing result_json and computed_at (stale cache survives failure).
        Creates the row if it does not exist yet.

        Args:
            error_message: Human-readable error description. Must not be None.

        Raises:
            ValueError: If error_message is None.
        """
        if error_message is None:
            raise ValueError("error_message must not be None")
        now = datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO dependency_map_dashboard_cache
                    (cache_key, result_json, computed_at, job_id,
                     last_failure_message, last_failure_at)
                VALUES (?, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    job_id = NULL,
                    last_failure_message = excluded.last_failure_message,
                    last_failure_at = excluded.last_failure_at
                """,
                (self._CACHE_KEY, error_message, now),
            )

        self._conn_manager.execute_atomic(_op)

    def clear_job_slot_for_retry(self) -> None:
        """
        Clear job_id and failure fields to allow a clean retry.

        Preserves result_json and computed_at so stale cache remains available.
        """

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE dependency_map_dashboard_cache
                SET job_id = NULL,
                    last_failure_message = NULL,
                    last_failure_at = NULL
                WHERE cache_key = ?
                """,
                (self._CACHE_KEY,),
            )

        self._conn_manager.execute_atomic(_op)

    def _insert_row_with_job_id(self, conn: sqlite3.Connection, job_id: str) -> None:
        """Insert a fresh cache row holding only job_id (all else NULL).

        Shared by claim_job_slot and set_job_slot to avoid duplicating
        this INSERT (both create the row from scratch when none exists).
        """
        conn.execute(
            """
            INSERT INTO dependency_map_dashboard_cache
                (cache_key, result_json, computed_at, job_id,
                 last_failure_message, last_failure_at)
            VALUES (?, NULL, NULL, ?, NULL, NULL)
            """,
            (self._CACHE_KEY, job_id),
        )

    def claim_job_slot(self, new_job_id: str) -> Optional[str]:
        """
        Atomic compare-and-swap: claim the job slot if currently empty.

        Args:
            new_job_id: The job ID to claim.

        Returns:
            None if the claim succeeded (slot was empty).
            The existing job_id string if the slot was already taken.
        """
        result: List[Optional[str]] = [None]

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "SELECT job_id FROM dependency_map_dashboard_cache WHERE cache_key = ?",
                (self._CACHE_KEY,),
            )
            row = cursor.fetchone()

            if row is None:
                self._insert_row_with_job_id(conn, new_job_id)
                result[0] = None
                return

            existing_job_id = row[0]
            if existing_job_id is not None:
                result[0] = existing_job_id
                return

            conn.execute(
                "UPDATE dependency_map_dashboard_cache SET job_id = ? WHERE cache_key = ?",
                (new_job_id, self._CACHE_KEY),
            )
            result[0] = None

        self._conn_manager.execute_atomic(_op)
        return result[0]

    def set_job_slot(self, job_id: str, expected_current: Optional[str]) -> bool:
        """
        Compare-and-swap: re-point the job slot at job_id, but only if the
        slot currently holds expected_current (None means "no row, or an
        empty/NULL job_id").

        Exists to correct a placeholder job id to the real id returned by
        BackgroundJobManager.submit_job() (Bug #1620), since submit_job()
        mints its own job_id and ignores any id the caller pre-generated.

        This is deliberately a CAS -- not an unconditional overwrite --
        because a concurrent request can legitimately change the slot
        between the caller's claim_job_slot(placeholder) and this call
        (e.g. clearing a perceived zombie, or caching a completed result).
        Blindly overwriting that state would clobber a legitimate
        transition; instead the swap no-ops and logs a WARNING.

        Args:
            job_id: The real job ID to record in the slot.
            expected_current: The job id the caller believes the slot
                currently holds (typically its own placeholder), or None to
                mean "the slot is currently empty / no row exists yet".

        Returns:
            True if the swap was applied, False if the slot no longer held
            expected_current (no write performed).
        """
        result: List[bool] = [False]

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "SELECT job_id FROM dependency_map_dashboard_cache WHERE cache_key = ?",
                (self._CACHE_KEY,),
            )
            row = cursor.fetchone()

            if row is None:
                if expected_current is not None:
                    result[0] = False
                    return
                self._insert_row_with_job_id(conn, job_id)
                result[0] = True
                return

            current_job_id = row[0]
            if current_job_id != expected_current:
                result[0] = False
                return

            conn.execute(
                "UPDATE dependency_map_dashboard_cache SET job_id = ? WHERE cache_key = ?",
                (job_id, self._CACHE_KEY),
            )
            result[0] = True

        self._conn_manager.execute_atomic(_op)
        if not result[0]:
            logger.warning(
                "DependencyMapDashboardCacheBackend.set_job_slot: CAS failed "
                "-- slot does not hold expected_current=%r; job_id=%r not applied",
                expected_current,
                job_id,
            )
        return result[0]

    def get_running_job_id(self, job_tracker: Any = None) -> Optional[str]:
        """
        Return the current job_id if a job is actively running, else None.

        If job_tracker is provided, verifies the job is still alive. A job whose
        status is not in ('running', 'pending', 'queued') is considered a zombie:
        the slot is cleared and None is returned.

        When job_tracker.get_job() raises, the exception is logged as a warning
        and job_id is returned conservatively (unavailable tracker should not
        incorrectly evict a legitimately running job).

        Args:
            job_tracker: Optional object with get_job(job_id) returning an object
                         with a .status attribute, or None.

        Returns:
            job_id string if a live job is running, None otherwise.
        """
        cached = self.get_cached()
        if cached is None:
            return None
        job_id = cached.get("job_id")
        if job_id is None:
            return None

        if job_tracker is None:
            return str(job_id)

        try:
            job = job_tracker.get_job(job_id)
            if job is None or job.status not in ("running", "pending", "queued"):
                self.clear_job_slot()
                return None
        except Exception as exc:
            logger.warning(
                "DependencyMapDashboardCacheBackend.get_running_job_id: "
                "job_tracker.get_job(%r) raised, treating job as still running: %s",
                job_id,
                exc,
            )
            return str(job_id)

        return str(job_id)
