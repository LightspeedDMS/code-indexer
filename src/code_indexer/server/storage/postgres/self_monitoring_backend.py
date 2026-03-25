"""
PostgreSQL backend for self-monitoring storage (Story #524).

Drop-in replacement for SelfMonitoringSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the SelfMonitoringBackend Protocol
(protocols.py).

Tables created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class SelfMonitoringPostgresBackend:
    """
    PostgreSQL backend for self-monitoring storage.

    Satisfies the SelfMonitoringBackend Protocol (protocols.py).
    All mutations commit immediately after DML execution.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure tables exist.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create self_monitoring tables and indexes if they do not already exist."""
        try:
            with self._pool.connection() as conn:
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
                        id SERIAL PRIMARY KEY,
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
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sm_scans_started_at ON self_monitoring_scans(started_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sm_issues_created_at ON self_monitoring_issues(created_at)"
                )
                conn.commit()
        except Exception as exc:
            logger.warning(
                "SelfMonitoringPostgresBackend: schema setup failed: %s", exc
            )

    def create_scan_record(
        self,
        scan_id: str,
        started_at: str,
        log_id_start: int,
    ) -> None:
        """Insert initial scan record with RUNNING status."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end, issues_created) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (scan_id, started_at, "RUNNING", log_id_start, log_id_start, 0),
            )
            conn.commit()

    def get_last_scan_log_id(self) -> int:
        """Return log_id_end from most recent SUCCESS scan, or 0."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT log_id_end FROM self_monitoring_scans "
                "WHERE status = 'SUCCESS' AND log_id_end IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
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
        update_fields = ["status = %s", "completed_at = %s"]
        update_values: List[Any] = [status, completed_at]

        if log_id_end is not None:
            update_fields.append("log_id_end = %s")
            update_values.append(log_id_end)
        if issues_created is not None:
            update_fields.append("issues_created = %s")
            update_values.append(issues_created)
        if error_message is not None:
            update_fields.append("error_message = %s")
            update_values.append(error_message)

        update_values.append(scan_id)
        query = f"UPDATE self_monitoring_scans SET {', '.join(update_fields)} WHERE scan_id = %s"

        with self._pool.connection() as conn:
            conn.execute(query, update_values)
            conn.commit()

    def cleanup_orphaned_scans(self, cutoff_iso: str) -> int:
        """Mark scans started before cutoff_iso with no completed_at as FAILURE.

        Returns count of scans updated.
        """
        with self._pool.connection() as conn:
            result = conn.execute(
                "UPDATE self_monitoring_scans SET status = 'FAILURE', error_message = 'Orphaned scan' "
                "WHERE started_at < %s AND completed_at IS NULL",
                (cutoff_iso,),
            )
            count = int(result.rowcount) if result.rowcount else 0
            conn.commit()
        return count

    def get_last_started_at(self) -> Optional[str]:
        """Return started_at from most recent scan (any status), or None."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT started_at FROM self_monitoring_scans ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None  # type: ignore[no-any-return]

    def fetch_stored_fingerprints(
        self, retention_days: int
    ) -> List[Tuple[str, str, str, str, str]]:
        """Return fingerprint rows (fingerprint, classification, error_codes, title, created_at)."""
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT fingerprint, classification, error_codes, title, created_at "
                "FROM self_monitoring_issues "
                "WHERE created_at >= %s "
                "ORDER BY created_at DESC",
                (cutoff,),
            ).fetchall()
        return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]

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
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO self_monitoring_issues "
                "(scan_id, github_issue_number, github_issue_url, classification, "
                "error_codes, fingerprint, source_log_ids, source_files, title, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            conn.commit()

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
