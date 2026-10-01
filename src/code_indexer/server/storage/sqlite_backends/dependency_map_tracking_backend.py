"""
SQLite backend for dependency map tracking (Story #192).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

# Sentinel value for distinguishing "not provided" from "explicitly None"
_UNSET: Any = object()


class DependencyMapTrackingBackend:
    """
    SQLite backend for dependency map tracking (Story #192).

    Uses a singleton row (id=1) to track dependency map analysis state.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def get_tracking(self) -> Dict[str, Any]:
        """
        Get the singleton tracking record.

        Initializes the singleton row if it doesn't exist.
        Also ensures run_history table exists for AC9 compatibility.
        Also ensures refinement columns exist for Story #359 compatibility.

        Returns:
            Dictionary with tracking data (id, last_run, next_run, status,
            commit_hashes, error_message, refinement_cursor, refinement_next_run)
        """
        self._ensure_run_history_table()
        self._ensure_refinement_columns()
        conn = self._conn_manager.get_connection()

        # Try to fetch existing singleton row
        cursor = conn.execute(
            """SELECT id, last_run, next_run, status, commit_hashes, error_message,
                      refinement_cursor, refinement_next_run
               FROM dependency_map_tracking WHERE id = 1"""
        )
        row = cursor.fetchone()

        if row is None:
            # Initialize singleton row
            def operation(conn):
                conn.execute(
                    """INSERT INTO dependency_map_tracking (id, status)
                       VALUES (1, 'pending')"""
                )
                return None

            self._conn_manager.execute_atomic(operation)

            # Fetch newly created row
            cursor = conn.execute(
                """SELECT id, last_run, next_run, status, commit_hashes, error_message,
                          refinement_cursor, refinement_next_run
                   FROM dependency_map_tracking WHERE id = 1"""
            )
            row = cursor.fetchone()

        return {
            "id": row[0],
            "last_run": row[1],
            "next_run": row[2],
            "status": row[3],
            "commit_hashes": row[4],
            "error_message": row[5],
            "refinement_cursor": row[6],
            "refinement_next_run": row[7],
        }

    def update_tracking(
        self,
        last_run: Optional[str] = _UNSET,
        next_run: Optional[str] = _UNSET,
        status: Optional[str] = _UNSET,
        commit_hashes: Optional[str] = _UNSET,
        error_message: Optional[str] = _UNSET,
        refinement_cursor: Optional[int] = _UNSET,
        refinement_next_run: Optional[str] = _UNSET,
    ) -> None:
        """
        Update the singleton tracking record.

        Only updates fields that are explicitly provided (partial updates supported).

        Args:
            last_run: ISO timestamp of last analysis run
            next_run: ISO timestamp of next scheduled run
            status: Analysis status (pending/running/completed/failed)
            commit_hashes: JSON string mapping repo alias to commit hash
            error_message: Error message if analysis failed (None clears the error)
            refinement_cursor: Index of next domain to refine (Story #359)
            refinement_next_run: ISO timestamp of next refinement cycle (Story #359)
        """
        # Build UPDATE statement for provided fields only
        updates: list[str] = []
        params: list[Any] = []

        if last_run is not _UNSET:
            updates.append("last_run = ?")
            params.append(last_run)

        if next_run is not _UNSET:
            updates.append("next_run = ?")
            params.append(next_run)

        if status is not _UNSET:
            updates.append("status = ?")
            params.append(status)

        if commit_hashes is not _UNSET:
            updates.append("commit_hashes = ?")
            params.append(commit_hashes)

        if error_message is not _UNSET:
            updates.append("error_message = ?")
            params.append(error_message)

        if refinement_cursor is not _UNSET:
            updates.append("refinement_cursor = ?")
            params.append(refinement_cursor)

        if refinement_next_run is not _UNSET:
            updates.append("refinement_next_run = ?")
            params.append(refinement_next_run)

        if not updates:
            return  # No fields to update

        def operation(conn):
            conn.execute(
                f"UPDATE dependency_map_tracking SET {', '.join(updates)} WHERE id = 1",
                params,
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug("Updated dependency map tracking record")

    def cleanup_stale_status_on_startup(self) -> bool:
        """Reset stale running/pending status to failed on server startup.

        Called once during server startup. If the singleton row has status
        'running' or 'pending', the previous server process was killed
        mid-analysis. Reset to 'failed' so new jobs can be triggered.

        Returns:
            True if a stale status was cleaned up, False otherwise.
        """

        def operation(conn):
            cursor = conn.execute(
                "SELECT status FROM dependency_map_tracking WHERE id = 1"
            )
            row = cursor.fetchone()
            if row is None:
                return False
            status = row[0]
            if status in ("running", "pending"):
                conn.execute(
                    "UPDATE dependency_map_tracking SET status = 'failed', "
                    "error_message = 'orphaned - server restarted' "
                    "WHERE id = 1",
                )
                return True
            return False

        cleaned = self._conn_manager.execute_atomic(operation)
        if cleaned:
            logger.info(
                "DependencyMapTrackingBackend: reset stale status to 'failed' on startup"
            )
        return bool(cleaned)

    def _ensure_run_history_table(self) -> None:
        """Ensure dependency_map_run_history table exists (idempotent).

        Also ensures the parent dependency_map_tracking table exists
        so this backend works in test databases created without initialize_database().
        """

        def operation(conn):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dependency_map_tracking (
                    id INTEGER PRIMARY KEY,
                    last_run TEXT,
                    next_run TEXT,
                    status TEXT DEFAULT 'pending',
                    commit_hashes TEXT,
                    error_message TEXT
                )
            """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dependency_map_run_history (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    domain_count INTEGER,
                    total_chars INTEGER,
                    edge_count INTEGER,
                    zero_char_domains INTEGER,
                    repos_analyzed INTEGER,
                    repos_skipped INTEGER,
                    pass1_duration_s REAL,
                    pass2_duration_s REAL,
                    run_type TEXT,
                    phase_timings_json TEXT
                )
            """
            )
            # Bug #874 Story B: idempotent ALTER TABLE for existing installations.
            # SQLite has no ADD COLUMN IF NOT EXISTS, so catch duplicate-column errors.
            for col_ddl in [
                "ALTER TABLE dependency_map_run_history ADD COLUMN run_type TEXT",
                "ALTER TABLE dependency_map_run_history ADD COLUMN phase_timings_json TEXT",
            ]:
                try:
                    conn.execute(col_ddl)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            return None

        self._conn_manager.execute_atomic(operation)

    def _ensure_refinement_columns(self) -> None:
        """Add refinement tracking columns if they don't exist (Story #359).

        Idempotent: safe to call on both new and existing databases.
        Uses ALTER TABLE for backward-compatible schema migration.
        Probes each column independently to handle half-migration scenarios.
        """

        def _do_migrate(conn: sqlite3.Connection) -> None:
            for col, col_type in [
                ("refinement_cursor", "INTEGER DEFAULT 0"),
                ("refinement_next_run", "TEXT"),
            ]:
                try:
                    conn.execute(f"SELECT {col} FROM dependency_map_tracking LIMIT 1")
                except sqlite3.OperationalError:
                    conn.execute(
                        f"ALTER TABLE dependency_map_tracking ADD COLUMN {col} {col_type}"
                    )

        self._conn_manager.execute_atomic(_do_migrate)

    def record_run_metrics(
        self,
        metrics: Dict[str, Any],
        run_type: Optional[str] = None,
        phase_timings_json: Optional[str] = None,
    ) -> None:
        """
        Store run metrics to dependency_map_run_history (AC9, Story #216).

        Args:
            metrics: Dict with keys: timestamp, domain_count, total_chars, edge_count,
                     zero_char_domains, repos_analyzed, repos_skipped,
                     pass1_duration_s, pass2_duration_s
            run_type: Optional run classification string (e.g. "delta", "full").
                      Bug #874 Story B. NULL for legacy rows.
            phase_timings_json: Optional pre-serialized JSON string with per-phase
                      timing breakdown. Bug #874 Story B. NULL for legacy rows.
        """
        self._ensure_run_history_table()

        def operation(conn):
            conn.execute(
                """INSERT INTO dependency_map_run_history
                   (timestamp, domain_count, total_chars, edge_count, zero_char_domains,
                    repos_analyzed, repos_skipped, pass1_duration_s, pass2_duration_s,
                    run_type, phase_timings_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metrics.get("timestamp"),
                    metrics.get("domain_count"),
                    metrics.get("total_chars"),
                    metrics.get("edge_count"),
                    metrics.get("zero_char_domains"),
                    metrics.get("repos_analyzed"),
                    metrics.get("repos_skipped"),
                    metrics.get("pass1_duration_s"),
                    metrics.get("pass2_duration_s"),
                    run_type,
                    phase_timings_json,
                ),
            )
            return None

        self._conn_manager.execute_atomic(operation)
        logger.debug("Recorded dependency map run metrics")

    def get_run_history(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Retrieve recent run metrics ordered most-recent-first (AC9, Story #216).

        Args:
            limit: Maximum number of records to return (default 5)

        Returns:
            List of metric dicts ordered by run_id descending (most recent first)
        """
        self._ensure_run_history_table()
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT run_id, timestamp, domain_count, total_chars, edge_count,
                      zero_char_domains, repos_analyzed, repos_skipped,
                      pass1_duration_s, pass2_duration_s,
                      run_type, phase_timings_json
               FROM dependency_map_run_history
               ORDER BY run_id DESC
               LIMIT ?""",
            (limit,),
        )
        rows = cursor.fetchall()
        return [
            {
                "run_id": row[0],
                "timestamp": row[1],
                "domain_count": row[2],
                "total_chars": row[3],
                "edge_count": row[4],
                "zero_char_domains": row[5],
                "repos_analyzed": row[6],
                "repos_skipped": row[7],
                "pass1_duration_s": row[8],
                "pass2_duration_s": row[9],
                "run_type": row[10],
                "phase_timings_json": row[11],
            }
            for row in rows
        ]

    def cleanup_old_history(self, cutoff_iso: str) -> int:
        """Delete dependency_map_run_history records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of deleted records.
        """
        self._ensure_run_history_table()
        deleted = 0

        def operation(conn):
            nonlocal deleted
            cursor = conn.execute(
                "DELETE FROM dependency_map_run_history WHERE timestamp < ?",
                (cutoff_iso,),
            )
            deleted = cursor.rowcount

        self._conn_manager.execute_atomic(operation)
        return deleted

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
