"""
SQLite backend for self-monitoring storage (Story #524).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager


class SelfMonitoringSqliteBackend:
    """
    SQLite backend for self-monitoring storage (Story #524).

    Satisfies the SelfMonitoringBackend Protocol.
    Uses the main cidx_server.db (self_monitoring_scans and
    self_monitoring_issues tables).
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create self_monitoring tables if they do not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS self_monitoring_scans (
                    scan_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    log_id_start INTEGER NOT NULL,
                    log_id_end INTEGER,
                    completed_at TEXT,
                    issues_created INTEGER,
                    error_message TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS self_monitoring_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id TEXT NOT NULL,
                    github_issue_number INTEGER,
                    github_issue_url TEXT,
                    classification TEXT NOT NULL,
                    title TEXT NOT NULL,
                    error_codes TEXT,
                    fingerprint TEXT NOT NULL,
                    source_log_ids TEXT,
                    source_files TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )

        self._conn_manager.execute_atomic(_op)

    def create_scan_record(
        self,
        scan_id: str,
        started_at: str,
        log_id_start: int,
    ) -> None:
        """Insert initial scan record with RUNNING status."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end, issues_created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (scan_id, started_at, "RUNNING", log_id_start, log_id_start, 0),
            )

        self._conn_manager.execute_atomic(_op)

    def get_last_scan_log_id(self) -> int:
        """Return log_id_end from most recent SUCCESS scan, or 0."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT log_id_end FROM self_monitoring_scans "
            "WHERE status = 'SUCCESS' AND log_id_end IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else 0  # type: ignore[no-any-return]

    def update_scan_record(
        self,
        scan_id: str,
        status: str,
        completed_at: str,
        log_id_end: Optional[int] = None,
        issues_created: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update scan record with completion status and metrics."""
        update_fields = ["status = ?", "completed_at = ?"]
        update_values: List[Any] = [status, completed_at]

        if log_id_end is not None:
            update_fields.append("log_id_end = ?")
            update_values.append(log_id_end)
        if issues_created is not None:
            update_fields.append("issues_created = ?")
            update_values.append(issues_created)
        if error_message is not None:
            update_fields.append("error_message = ?")
            update_values.append(error_message)

        update_values.append(scan_id)
        query = f"UPDATE self_monitoring_scans SET {', '.join(update_fields)} WHERE scan_id = ?"

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(query, update_values)

        self._conn_manager.execute_atomic(_op)

    def cleanup_orphaned_scans(self, cutoff_iso: str) -> int:
        """Mark scans started before cutoff_iso with no completed_at as FAILURE.

        Returns count of scans updated.
        """
        # Pre-existing, unchanged: result accumulator dict is the same
        # pattern execute_atomic()'s callback-closure contract uses
        # throughout this module -- a nonlocal int would be equivalent;
        # kept as Dict[str, Any] to match the original relocated source.
        result: Dict[str, Any] = {"count": 0}

        def _op(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                "UPDATE self_monitoring_scans SET status = 'FAILURE', error_message = 'Orphaned scan' "
                "WHERE started_at < ? AND completed_at IS NULL",
                (cutoff_iso,),
            )
            result["count"] = cursor.rowcount

        self._conn_manager.execute_atomic(_op)
        return result["count"]  # type: ignore[no-any-return]

    def get_last_started_at(self) -> Optional[str]:
        """Return started_at from most recent scan (any status), or None."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT started_at FROM self_monitoring_scans ORDER BY started_at DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return row[0] if row else None  # type: ignore[no-any-return]

    def fetch_stored_fingerprints(
        self, retention_days: int
    ) -> List[Tuple[str, str, str, str, str]]:
        """Return fingerprint rows (fingerprint, classification, error_codes, title, created_at)."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT fingerprint, classification, error_codes, title, created_at "
            "FROM self_monitoring_issues "
            "WHERE datetime(created_at) >= datetime('now', '-' || ? || ' days') "
            "ORDER BY created_at DESC",
            (retention_days,),
        )
        return [(row[0], row[1], row[2], row[3], row[4]) for row in cursor.fetchall()]

    def store_issue_metadata(
        self,
        scan_id: str,
        github_issue_number: Optional[int],
        github_issue_url: Optional[str],
        classification: str,
        title: str,
        error_codes: str,
        fingerprint: str,
        source_log_ids: str,
        source_files: str,
        created_at: str,
    ) -> None:
        """Persist issue metadata in self_monitoring_issues."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO self_monitoring_issues "
                "(scan_id, github_issue_number, github_issue_url, classification, "
                "error_codes, fingerprint, source_log_ids, source_files, title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    github_issue_number,
                    github_issue_url,
                    classification,
                    error_codes,
                    fingerprint,
                    source_log_ids,
                    source_files,
                    title,
                    created_at,
                ),
            )

        self._conn_manager.execute_atomic(_op)

    def list_scans(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return scan history records, most recent first."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """
            SELECT scan_id, started_at, completed_at, status,
                   log_id_start, log_id_end, issues_created, error_message
            FROM self_monitoring_scans
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [
            "scan_id",
            "started_at",
            "completed_at",
            "status",
            "log_id_start",
            "log_id_end",
            "issues_created",
            "error_message",
        ]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def list_issues(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return issue records, most recent first."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """
            SELECT id, scan_id, github_issue_number, github_issue_url,
                   classification, title, fingerprint,
                   source_log_ids, source_files, created_at
            FROM self_monitoring_issues
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [
            "id",
            "scan_id",
            "github_issue_number",
            "github_issue_url",
            "classification",
            "title",
            "fingerprint",
            "source_log_ids",
            "source_files",
            "created_at",
        ]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_running_scan_count(self) -> int:
        """Return count of scans where completed_at IS NULL."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM self_monitoring_scans WHERE completed_at IS NULL"
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()
