"""
SQLite backend for sync job management.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class SyncJobsSqliteBackend:
    """
    SQLite backend for sync job management.

    Replaces JSON file storage with atomic SQLite operations.
    Complex nested data (phases, analytics) stored as JSON blobs.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the backend."""
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def create_job(
        self,
        job_id: str,
        username: str,
        user_alias: str,
        job_type: str,
        status: str,
        repository_url: Optional[str] = None,
    ) -> None:
        """Create a new sync job."""
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                """INSERT INTO sync_jobs
                   (job_id, username, user_alias, job_type, status, created_at, repository_url, progress)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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
            return None

        self._conn_manager.execute_atomic(operation)
        logger.info(f"Created sync job: {job_id}")

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job details by job ID."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT job_id, username, user_alias, job_type, status, created_at,
                      started_at, completed_at, repository_url, progress, error_message,
                      phases, phase_weights, current_phase, progress_history,
                      recovery_checkpoint, analytics_data
               FROM sync_jobs WHERE job_id = ?""",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def _row_to_dict(self, row) -> Dict[str, Any]:
        """Convert a database row to job dictionary."""
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

    def update_job(self, job_id: str, **kwargs) -> None:
        """Update job fields. Accepts: status, progress, error_message, phases, etc."""
        json_fields = {
            "phases",
            "phase_weights",
            "progress_history",
            "recovery_checkpoint",
            "analytics_data",
        }
        updates, params = [], []
        for key, value in kwargs.items():
            if value is not None:
                updates.append(f"{key} = ?")
                params.append(json.dumps(value) if key in json_fields else value)
        if not updates:
            return
        params.append(job_id)

        def operation(conn):
            conn.execute(
                f"UPDATE sync_jobs SET {', '.join(updates)} WHERE job_id = ?", params
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def list_jobs(self) -> list:
        """List all sync jobs."""
        # Bug #1532 follow-up: route the raw connection through
        # guarded_connection() so close_all() cannot close it mid-read.
        # Correction to commit e5723217's message: this file has 82 OTHER
        # bare-connection-fetch call sites (self._conn_manager plus the
        # bare-fetch method name) beyond this one, not "~100+" as that
        # commit message states -- history is immutable this deep in the
        # chain, so the accurate count is recorded here instead. Still
        # deliberately out of scope for a dedicated sweep, per that
        # commit's own rationale.
        with self._conn_manager.guarded_connection() as conn:
            fetched = conn.execute(
                """SELECT job_id, username, user_alias, job_type, status, created_at,
                          started_at, completed_at, repository_url, progress, error_message,
                          phases, phase_weights, current_phase, progress_history,
                          recovery_checkpoint, analytics_data FROM sync_jobs"""
            ).fetchall()
        return [self._row_to_dict(row) for row in fetched]

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID."""

        def operation(conn):
            cursor = conn.execute("DELETE FROM sync_jobs WHERE job_id = ?", (job_id,))
            return cursor.rowcount > 0

        deleted: bool = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.info(f"Deleted sync job: {job_id}")
        return deleted

    def cleanup_orphaned_jobs_on_startup(self) -> int:
        """
        Clean up orphaned sync jobs on server startup.

        On server restart, any sync jobs with status 'running' or 'pending' are
        orphaned because the threads executing them no longer exist. This method
        marks them as 'failed' with an appropriate error message and timestamp
        for audit trail.

        Bug #436: Orphaned jobs persist as "running" after server restart.

        Bug #1563 scope note: this method is UNCONDITIONALLY unscoped
        (no node or worker identity check at all), the same class of
        hazard fixed for BackgroundJobsSqliteBackend/
        BackgroundJobsPostgresBackend above/elsewhere in this module.
        It is deliberately NOT given the same worker-pid-liveness fix
        here: the `sync_jobs` table has no owning-node or owning-worker
        identity column at all (unlike `background_jobs`'s
        executing_node/executing_pid), so there is nothing to check
        liveness against. Adding one would require a schema change in
        storage/database_manager.py (this table's schema owner) and a
        caller change in jobs/manager.py's SyncJobManager (the only
        production caller, which stamps no owner today) -- both outside
        this fix's authorized file scope. In practice this is a solo-only
        code path today (SyncJobManager only ever constructs
        self._sqlite_backend, never a PostgreSQL sync-jobs backend), so
        the multi-worker recycle scenario this bug describes does not
        currently reach it; left here as an accurate, honest scope
        boundary rather than a silent gap.

        Returns:
            Number of orphaned jobs that were cleaned up.
        """
        interrupted_at = datetime.now(timezone.utc).isoformat()
        error_message = "Job interrupted by server restart"

        def operation(conn):
            cursor = conn.execute(
                """UPDATE sync_jobs
                   SET status = 'failed',
                       error_message = ?,
                       completed_at = ?
                   WHERE status IN ('running', 'pending')""",
                (error_message, interrupted_at),
            )
            return cursor.rowcount

        count: int = self._conn_manager.execute_atomic(operation)
        if count > 0:
            logger.info(
                f"SyncJobsSqliteBackend.cleanup_orphaned_jobs_on_startup: "
                f"marked {count} orphaned sync job(s) as failed"
            )
        return count

    def cleanup_old_completed(self, cutoff_iso: str) -> int:
        """Delete completed or failed sync jobs older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; jobs with completed_at before this
                        value and status IN ('completed', 'failed') are deleted.

        Returns:
            Number of rows deleted.
        """
        total_deleted = 0

        def operation(conn) -> int:
            cursor = conn.execute(
                """DELETE FROM sync_jobs
                   WHERE rowid IN (
                       SELECT rowid FROM sync_jobs
                       WHERE completed_at < ?
                         AND status IN ('completed', 'failed')
                       LIMIT 1000
                   )""",
                (cutoff_iso,),
            )
            return cursor.rowcount  # type: ignore[no-any-return]

        while True:
            batch: int = self._conn_manager.execute_atomic(operation)
            if batch == 0:
                break
            total_deleted += batch

        return total_deleted

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
