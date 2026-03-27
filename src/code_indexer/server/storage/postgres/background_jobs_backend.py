"""
PostgreSQL backend for background job management.

Story #413: PostgreSQL Backend for BackgroundJobs and SyncJobs

Drop-in replacement for BackgroundJobsSqliteBackend that satisfies the
BackgroundJobsBackend Protocol defined in storage/protocols.py.

Uses psycopg v3 via the ConnectionPool from connection_pool.py.
All JSON-valued columns (result, claude_actions, extended_error,
language_resolution_status) are serialised/deserialised with json.dumps/loads.
Boolean columns (is_admin, cancelled) are stored as native PG BOOLEAN.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

_ALLOWED_JOB_COLUMNS = frozenset(
    {
        "status",
        "progress",
        "error",
        "result",
        "completed_at",
        "started_at",
        "cancelled",
        "repo_alias",
        "resolution_attempts",
        "claude_actions",
        "failure_reason",
        "extended_error",
        "language_resolution_status",
        "progress_info",
        "metadata",
        "executing_node",
        "claimed_at",
        "current_phase",
        "phase_detail",
    }
)

# Columns selected in every SELECT query (ordered — must match _row_to_dict)
_SELECT_COLS = """
    job_id, operation_type, status, created_at, started_at, completed_at,
    result, error, progress, username, is_admin, cancelled, repo_alias,
    resolution_attempts, claude_actions, failure_reason, extended_error,
    language_resolution_status, progress_info, metadata
"""


class BackgroundJobsPostgresBackend:
    """
    PostgreSQL backend for background job management.

    Satisfies the BackgroundJobsBackend Protocol.  Intended as a drop-in
    replacement for BackgroundJobsSqliteBackend when the server is configured
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
        """Convert a psycopg row (sequence) to a job dictionary."""

        # Convert PG datetime objects to ISO strings for consistency with SQLite
        def _dt(val: Any) -> Any:
            from datetime import datetime as _dt_cls

            return val.isoformat() if isinstance(val, _dt_cls) else val

        # Handle JSONB columns: psycopg returns dicts, but strings from migration
        def _json_col(val: Any) -> Any:
            if val is None:
                return None
            if isinstance(val, (dict, list)):
                return val  # Already parsed by psycopg
            if isinstance(val, str):
                return json.loads(val)
            return val

        return {
            "job_id": row[0],
            "operation_type": row[1],
            "status": row[2],
            "created_at": _dt(row[3]),
            "started_at": _dt(row[4]),
            "completed_at": _dt(row[5]),
            "result": _json_col(row[6]),
            "error": row[7],
            "progress": row[8],
            "username": row[9],
            "is_admin": bool(row[10]),
            "cancelled": bool(row[11]),
            "repo_alias": row[12],
            "resolution_attempts": row[13],
            "claude_actions": _json_col(row[14]),
            "failure_reason": row[15],
            "extended_error": _json_col(row[16]),
            "language_resolution_status": _json_col(row[17]),
            "progress_info": row[18] if len(row) > 18 else None,
            "metadata": json.loads(row[19]) if len(row) > 19 and row[19] else None,
        }

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def save_job(
        self,
        job_id: str,
        operation_type: str,
        status: str,
        created_at: str,
        username: str,
        progress: int,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        is_admin: bool = False,
        cancelled: bool = False,
        repo_alias: Optional[str] = None,
        resolution_attempts: int = 0,
        claude_actions: Optional[List[str]] = None,
        failure_reason: Optional[str] = None,
        extended_error: Optional[Dict[str, Any]] = None,
        language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = None,
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert a new background job row."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO background_jobs (
                        job_id, operation_type, status, created_at, started_at,
                        completed_at, result, error, progress, username, is_admin,
                        cancelled, repo_alias, resolution_attempts, claude_actions,
                        failure_reason, extended_error, language_resolution_status,
                        progress_info, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s
                    )
                    """,
                    (
                        job_id,
                        operation_type,
                        status,
                        created_at,
                        started_at,
                        completed_at,
                        json.dumps(result) if result is not None else None,
                        error,
                        progress,
                        username,
                        is_admin,
                        cancelled,
                        repo_alias,
                        resolution_attempts,
                        json.dumps(claude_actions)
                        if claude_actions is not None
                        else None,
                        failure_reason,
                        json.dumps(extended_error)
                        if extended_error is not None
                        else None,
                        json.dumps(language_resolution_status)
                        if language_resolution_status is not None
                        else None,
                        progress_info,
                        json.dumps(metadata) if metadata is not None else None,
                    ),
                )
        logger.debug("Saved background job: %s", job_id)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return job dict by job_id, or None if not found."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM background_jobs WHERE job_id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        """Update arbitrary columns on a background job row."""
        _JSON_FIELDS = {
            "result",
            "claude_actions",
            "extended_error",
            "language_resolution_status",
        }
        updates: List[str] = []
        params: List[Any] = []

        for key, value in kwargs.items():
            if key not in _ALLOWED_JOB_COLUMNS:
                raise ValueError(f"Column {key!r} is not allowed")
            updates.append(f"{key} = %s")
            if value is None:
                params.append(None)
            elif key in _JSON_FIELDS:
                params.append(json.dumps(value))
            else:
                params.append(value)

        if not updates:
            return

        params.append(job_id)
        sql = f"UPDATE background_jobs SET {', '.join(updates)} WHERE job_id = %s"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)

    def list_jobs(
        self,
        username: Optional[str] = None,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List background jobs with optional filtering and pagination."""
        conditions: List[str] = []
        params: List[Any] = []

        if username:
            conditions.append("username = %s")
            params.append(username)
        if status:
            conditions.append("status = %s")
            params.append(status)
        if operation_type:
            conditions.append("operation_type = %s")
            params.append(operation_type)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            f"SELECT {_SELECT_COLS} FROM background_jobs"
            f"{where} ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_jobs_filtered(
        self,
        status: Optional[str] = None,
        operation_type: Optional[str] = None,
        search_text: Optional[str] = None,
        exclude_ids: Optional[Any] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> tuple:
        """
        Return (list_of_job_dicts, total_count) with dynamic WHERE filters.

        Mirrors BackgroundJobsSqliteBackend.list_jobs_filtered() behaviour.
        """
        conditions: List[str] = []
        params: List[Any] = []

        if status:
            conditions.append("status = %s")
            params.append(status)
        if operation_type:
            conditions.append("operation_type = %s")
            params.append(operation_type)
        if search_text:
            like = f"%{search_text}%"
            conditions.append(
                "(LOWER(repo_alias) LIKE LOWER(%s)"
                " OR LOWER(username) LIKE LOWER(%s)"
                " OR LOWER(operation_type) LIKE LOWER(%s)"
                " OR LOWER(COALESCE(error, '')) LIKE LOWER(%s))"
            )
            params.extend([like, like, like, like])
        if exclude_ids:
            exclude_list = list(exclude_ids)
            placeholders = ", ".join(["%s"] * len(exclude_list))
            conditions.append(f"job_id NOT IN ({placeholders})")
            params.extend(exclude_list)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

        # Total count (ignores limit/offset)
        count_sql = f"SELECT COUNT(*) FROM background_jobs{where}"
        data_sql = f"SELECT {_SELECT_COLS} FROM background_jobs{where} ORDER BY created_at DESC"
        data_params = list(params)

        if limit is not None:
            data_sql += " LIMIT %s OFFSET %s"
            data_params.extend([limit, offset])

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_sql, params)
                total_count: int = cur.fetchone()[0]
                cur.execute(data_sql, data_params)
                rows = cur.fetchall()

        jobs = [self._row_to_dict(r) for r in rows]
        return jobs, total_count

    def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID. Returns True if a row was deleted."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM background_jobs WHERE job_id = %s", (job_id,))
                deleted: bool = cur.rowcount > 0
        if deleted:
            logger.debug("Deleted background job: %s", job_id)
        return deleted

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """Delete old completed/failed/cancelled jobs older than max_age_hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff.isoformat()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM background_jobs
                    WHERE status IN ('completed', 'failed', 'cancelled')
                      AND completed_at IS NOT NULL
                      AND completed_at < %s
                    """,
                    (cutoff_iso,),
                )
                count: int = cur.rowcount
        if count > 0:
            logger.info("Cleaned up %d old background jobs", count)
        return count

    def count_jobs_by_status(self) -> Dict[str, int]:
        """Return a dict mapping status -> count for all jobs."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM background_jobs GROUP BY status"
                )
                rows = cur.fetchall()
        return {row[0]: row[1] for row in rows}

    def get_job_stats(self, time_filter: str = "24h") -> Dict[str, int]:
        """Return completed/failed counts for jobs within the specified time window."""
        now = datetime.now(timezone.utc)
        if time_filter == "7d":
            cutoff = now - timedelta(days=7)
        elif time_filter == "30d":
            cutoff = now - timedelta(days=30)
        else:
            cutoff = now - timedelta(hours=24)

        cutoff_iso = cutoff.isoformat()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, COUNT(*)
                    FROM background_jobs
                    WHERE completed_at IS NOT NULL AND completed_at >= %s
                    GROUP BY status
                    """,
                    (cutoff_iso,),
                )
                rows = cur.fetchall()

        stats = {"completed": 0, "failed": 0}
        for row in rows:
            if row[0] in stats:
                stats[row[0]] = row[1]
        return stats

    def cleanup_orphaned_jobs_on_startup(self, node_id: Optional[str] = None) -> int:
        """
        Mark running/pending jobs as failed on server startup.

        Any job still in 'running' or 'pending' state when the server starts
        was orphaned by a previous crash or restart.

        Returns:
            Number of orphaned jobs cleaned up.
        """
        if node_id is None:
            # Bug #535: In cluster mode, cleaning ALL jobs is dangerous —
            # it kills jobs on healthy nodes during rolling restarts.
            # Return 0 (no-op) as a safe default when node_id is not provided.
            logger.warning(
                "cleanup_orphaned_jobs_on_startup called without node_id — "
                "skipping cleanup to protect cross-node jobs"
            )
            return 0

        interrupted_at = datetime.now(timezone.utc).isoformat()
        error_message = "Job interrupted by server restart"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'failed',
                        error = %s,
                        completed_at = %s
                    WHERE status IN ('running', 'pending')
                      AND executing_node = %s
                    """,
                    (error_message, interrupted_at, node_id),
                )
                count: int = cur.rowcount
        if count > 0:
            logger.info("Cleaned up %d orphaned jobs on server startup", count)
        return count

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()
