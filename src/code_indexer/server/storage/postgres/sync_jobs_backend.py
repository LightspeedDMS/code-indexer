"""
PostgreSQL backend for sync job management.

Story #413: PostgreSQL Backend for BackgroundJobs and SyncJobs

Drop-in replacement for SyncJobsSqliteBackend that satisfies the
SyncJobsBackend Protocol defined in storage/protocols.py.

Uses psycopg v3 via the ConnectionPool from connection_pool.py.
JSON-valued columns (phases, phase_weights, progress_history,
recovery_checkpoint, analytics_data) are serialised/deserialised
with json.dumps/loads.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

_ALLOWED_SYNC_COLUMNS = frozenset(
    {
        "status",
        "progress",
        "current_file",
        "total_files",
        "error",
        "completed_at",
        "started_at",
        "cancelled",
        "phases",
        "phase_weights",
        "progress_history",
        "recovery_checkpoint",
        "analytics_data",
    }
)

# Columns selected in every SELECT query (ordered — must match _row_to_dict)
_SELECT_COLS = """
    job_id, username, user_alias, job_type, status, created_at,
    started_at, completed_at, repository_url, progress, error_message,
    phases, phase_weights, current_phase, progress_history,
    recovery_checkpoint, analytics_data
"""

_JSON_FIELDS = {
    "phases",
    "phase_weights",
    "progress_history",
    "recovery_checkpoint",
    "analytics_data",
}


class SyncJobsPostgresBackend:
    """
    PostgreSQL backend for sync job management.

    Satisfies the SyncJobsBackend Protocol.  Intended as a drop-in
    replacement for SyncJobsSqliteBackend when the server is configured
    to use PostgreSQL.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialise the backend with a shared connection pool.

        Args:
            pool: A ConnectionPool instance (from connection_pool.py).
        """
        self._pool = pool

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """Convert a psycopg row (sequence) to a sync job dictionary."""
        return {
            "job_id": row[0],
            "username": row[1],
            "user_alias": row[2],
            "job_type": row[3],
            "status": row[4],
            "created_at": row[5],
            "started_at": row[6],
            "completed_at": row[7],
            "repository_url": row[8],
            "progress": row[9],
            "error_message": row[10],
            "phases": json.loads(row[11]) if row[11] else None,
            "phase_weights": json.loads(row[12]) if row[12] else None,
            "current_phase": row[13],
            "progress_history": json.loads(row[14]) if row[14] else None,
            "recovery_checkpoint": json.loads(row[15]) if row[15] else None,
            "analytics_data": json.loads(row[16]) if row[16] else None,
        }

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def create_job(
        self,
        job_id: str,
        username: str,
        user_alias: str,
        job_type: str,
        status: str,
        repository_url: Optional[str] = None,
    ) -> None:
        """Insert a new sync job row."""
        now = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sync_jobs (
                        job_id, username, user_alias, job_type, status,
                        created_at, repository_url, progress
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        username,
                        user_alias,
                        job_type,
                        status,
                        now,
                        repository_url,
                        0,
                    ),
                )
        logger.info("Created sync job: %s", job_id)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return sync job dict by job_id, or None if not found."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM sync_jobs WHERE job_id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        """Update arbitrary columns on a sync job row."""
        updates: List[str] = []
        params: List[Any] = []

        for key, value in kwargs.items():
            if value is not None:
                if key not in _ALLOWED_SYNC_COLUMNS:
                    raise ValueError(f"Column {key!r} is not allowed")
                updates.append(f"{key} = %s")
                params.append(json.dumps(value) if key in _JSON_FIELDS else value)

        if not updates:
            return

        params.append(job_id)
        sql = f"UPDATE sync_jobs SET {', '.join(updates)} WHERE job_id = %s"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def list_jobs(self) -> list:
        """Return all sync jobs as a list of dicts."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_SELECT_COLS} FROM sync_jobs")
                rows = cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete_job(self, job_id: str) -> bool:
        """Delete a sync job by ID. Returns True if a row was deleted."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sync_jobs WHERE job_id = %s", (job_id,))
                deleted: bool = cur.rowcount > 0
        if deleted:
            logger.info("Deleted sync job: %s", job_id)
        return deleted

    def cleanup_orphaned_jobs_on_startup(self) -> int:
        """
        Mark running/pending sync jobs as failed on server startup.

        Any sync job still in 'running' or 'pending' state when the server
        starts was orphaned by a previous crash or restart.

        Returns:
            Number of orphaned jobs cleaned up.
        """
        interrupted_at = datetime.now(timezone.utc).isoformat()
        error_message = "Job interrupted by server restart"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sync_jobs
                    SET status = 'failed',
                        error_message = %s,
                        completed_at = %s
                    WHERE status IN ('running', 'pending')
                    """,
                    (error_message, interrupted_at),
                )
                count: int = cur.rowcount
        if count > 0:
            logger.info(
                "SyncJobsPostgresBackend.cleanup_orphaned_jobs_on_startup: "
                "marked %d orphaned sync job(s) as failed",
                count,
            )
        return count

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()
