"""
JobTracker: Standalone job tracking with hybrid in-memory + SQLite architecture.

Story #310: Epic #261 Story 1A - JobTracker Class, TrackedJob Dataclass, Schema Migration.

Provides:
- TrackedJob: Dataclass representing a tracked operation with full lifecycle state.
- DuplicateJobError: Raised when attempting to register a duplicate active job.
- JobTracker: Hybrid tracker using in-memory dict for O(1) active job lookups
  and SQLite background_jobs table for persistence and historical queries.
- TrackedOperation: Context manager for bounded operations with automatic
  status transitions (pending -> running -> completed/failed).
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TrackedJob dataclass
# ---------------------------------------------------------------------------


@dataclass
class TrackedJob:
    """
    Represents a tracked background operation.

    Fields map directly to columns in the background_jobs table.
    Optional fields default to None or 0 for numeric types.
    created_at is set automatically to UTC now when not provided.
    """

    job_id: str
    operation_type: str
    status: str  # "pending", "running", "completed", "failed"
    username: str
    repo_alias: Optional[str] = None
    progress: int = 0
    progress_info: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# DuplicateJobError exception
# ---------------------------------------------------------------------------


class DuplicateJobError(Exception):
    """
    Raised when attempting to register a job that conflicts with an existing
    active or pending job for the same (operation_type, repo_alias) pair.
    """

    def __init__(
        self,
        operation_type: str,
        repo_alias: Optional[str],
        existing_job_id: str,
    ) -> None:
        self.operation_type = operation_type
        self.repo_alias = repo_alias
        self.existing_job_id = existing_job_id
        super().__init__(
            f"Duplicate job: {operation_type} for {repo_alias} "
            f"(existing: {existing_job_id})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO-8601 string, or None."""
    if dt is None:
        return None
    return dt.isoformat()


def _iso_to_dt(s: Optional[str]) -> Optional[datetime]:
    """Parse ISO-8601 string to aware datetime, or None."""
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_tracked_job(row) -> TrackedJob:
    """
    Convert a SQLite row (SELECT *) from background_jobs to TrackedJob.

    Column order matches _SELECT_COLUMNS defined below.
    """
    (
        job_id,
        operation_type,
        status,
        created_at_str,
        started_at_str,
        completed_at_str,
        result_json,
        error,
        progress,
        username,
        repo_alias,
        progress_info,
        metadata_json,
    ) = row
    return TrackedJob(
        job_id=job_id,
        operation_type=operation_type,
        status=status,
        username=username,
        repo_alias=repo_alias,
        progress=progress or 0,
        progress_info=progress_info,
        metadata=json.loads(metadata_json) if metadata_json else None,
        created_at=_iso_to_dt(created_at_str) or datetime.now(timezone.utc),
        started_at=_iso_to_dt(started_at_str),
        completed_at=_iso_to_dt(completed_at_str),
        error=error,
        result=json.loads(result_json) if result_json else None,
    )


def _tracked_job_to_dict(job: TrackedJob) -> Dict[str, Any]:
    """Serialize TrackedJob to a plain dict (datetimes as ISO strings)."""
    return {
        "job_id": job.job_id,
        "operation_type": job.operation_type,
        "status": job.status,
        "username": job.username,
        "repo_alias": job.repo_alias,
        "progress": job.progress,
        "progress_info": job.progress_info,
        "metadata": job.metadata,
        "created_at": _dt_to_iso(job.created_at),
        "started_at": _dt_to_iso(job.started_at),
        "completed_at": _dt_to_iso(job.completed_at),
        "error": job.error,
        "result": job.result,
    }


# Columns fetched by all SELECT queries — keeps column index mapping single-source.
_SELECT_COLUMNS = (
    "job_id, operation_type, status, created_at, started_at, completed_at, "
    "result, error, progress, username, repo_alias, progress_info, metadata"
)


# ---------------------------------------------------------------------------
# JobTracker class
# ---------------------------------------------------------------------------


class JobTracker:
    """
    Hybrid job tracker: in-memory dict for active/pending, SQLite for history.

    Thread safety: All mutations to _active_jobs are protected by _lock.
    SQLite I/O is performed outside the lock to avoid holding it during I/O.

    Uses the EXISTING background_jobs table (no new tables).
    New columns progress_info and metadata are added by the schema migration
    _migrate_background_jobs_job_tracker() in DatabaseSchema.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialise tracker.

        Args:
            db_path: Path to the SQLite database file containing background_jobs.
        """
        self._conn_manager = DatabaseConnectionManager(db_path)
        self._active_jobs: Dict[str, TrackedJob] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_job(
        self,
        job_id: str,
        operation_type: str,
        username: str,
        repo_alias: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TrackedJob:
        """
        Register a new job with status "pending".

        Adds to in-memory dict immediately and persists to SQLite.

        Args:
            job_id: Unique job identifier (caller-generated UUID).
            operation_type: E.g. "dep_map_analysis", "description_refresh".
            username: User who initiated the job.
            repo_alias: Repository alias (optional, for repo-scoped operations).
            metadata: Arbitrary operation-specific context stored as JSON.

        Returns:
            The newly created TrackedJob.
        """
        job = TrackedJob(
            job_id=job_id,
            operation_type=operation_type,
            status="pending",
            username=username,
            repo_alias=repo_alias,
            metadata=metadata,
        )

        # Persist to SQLite first (outside lock)
        self._insert_job(job)

        # Add to memory
        with self._lock:
            self._active_jobs[job_id] = job

        logger.debug(
            f"JobTracker: registered job {job_id} ({operation_type}, repo={repo_alias})"
        )
        return job

    def update_status(
        self,
        job_id: str,
        status: Optional[str] = None,
        progress: Optional[int] = None,
        progress_info: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Update in-memory job and persist changes to SQLite.

        If status changes to "running", sets started_at automatically.

        Args:
            job_id: Job to update.
            status: New status value (optional).
            progress: New progress 0-100 (optional).
            progress_info: Human-readable progress description (optional).
            metadata: Replace job metadata (optional).
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            job = self._active_jobs.get(job_id)
            if job is None:
                logger.warning(
                    f"JobTracker.update_status: job {job_id} not in memory"
                )
                return

            if status is not None:
                job.status = status
                if status == "running" and job.started_at is None:
                    job.started_at = now
            if progress is not None:
                job.progress = progress
            if progress_info is not None:
                job.progress_info = progress_info
            if metadata is not None:
                job.metadata = metadata

            # Take a snapshot for SQLite update (outside lock)
            snapshot = TrackedJob(
                job_id=job.job_id,
                operation_type=job.operation_type,
                status=job.status,
                username=job.username,
                repo_alias=job.repo_alias,
                progress=job.progress,
                progress_info=job.progress_info,
                metadata=job.metadata,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                error=job.error,
                result=job.result,
            )

        self._upsert_job(snapshot)

    def complete_job(self, job_id: str, result: Optional[Dict[str, Any]] = None) -> None:
        """
        Mark job as completed.

        Sets status="completed" and completed_at, persists to SQLite,
        then removes from in-memory dict.

        Args:
            job_id: Job to complete.
            result: Optional result dict stored as JSON in SQLite.
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            job = self._active_jobs.pop(job_id, None)

        if job is None:
            logger.warning(f"JobTracker.complete_job: job {job_id} not in memory")
            return

        job.status = "completed"
        job.completed_at = now
        if result is not None:
            job.result = result

        self._upsert_job(job)
        logger.debug(f"JobTracker: completed job {job_id}")

    def fail_job(self, job_id: str, error: str) -> None:
        """
        Mark job as failed.

        Sets status="failed", completed_at, and error message.
        Persists to SQLite and removes from in-memory dict.

        Args:
            job_id: Job to fail.
            error: Human-readable error description.
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            job = self._active_jobs.pop(job_id, None)

        if job is None:
            logger.warning(f"JobTracker.fail_job: job {job_id} not in memory")
            return

        job.status = "failed"
        job.completed_at = now
        job.error = error

        self._upsert_job(job)
        logger.debug(f"JobTracker: failed job {job_id}: {error}")

    def get_job(self, job_id: str) -> Optional[TrackedJob]:
        """
        Return job by ID — checks memory first, falls back to SQLite.

        Args:
            job_id: Job identifier.

        Returns:
            TrackedJob or None if not found.
        """
        with self._lock:
            job = self._active_jobs.get(job_id)

        if job is not None:
            return job

        return self._load_job_from_sqlite(job_id)

    def get_active_jobs(self) -> List[TrackedJob]:
        """
        Return a snapshot of all in-memory active/pending jobs.

        Returns:
            List of TrackedJob instances (copies, not references).
        """
        with self._lock:
            return list(self._active_jobs.values())

    def get_recent_jobs(
        self,
        limit: int = 20,
        time_filter: str = "24h",
    ) -> List[Dict[str, Any]]:
        """
        Merge active in-memory jobs + recent historical jobs from SQLite.

        Active jobs are always included regardless of the time filter.
        Historical jobs are filtered by created_at within the time window.

        Args:
            limit: Maximum number of historical records from SQLite.
            time_filter: "1h", "24h", "7d", "30d", or "all".

        Returns:
            List of job dicts (most-recently-created first), deduped by job_id.
        """
        # Collect active jobs first (always included)
        with self._lock:
            active_snapshot = list(self._active_jobs.values())

        seen_ids = {j.job_id for j in active_snapshot}
        result = [_tracked_job_to_dict(j) for j in active_snapshot]

        # Compute cutoff
        cutoff_iso: Optional[str] = None
        if time_filter != "all":
            delta_map = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}
            hours = delta_map.get(time_filter, 24)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cutoff_iso = cutoff.isoformat()

        # Query SQLite for historical jobs
        conn = self._conn_manager.get_connection()
        if cutoff_iso:
            sql = (
                f"SELECT {_SELECT_COLUMNS} FROM background_jobs "
                "WHERE created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            cursor = conn.execute(sql, (cutoff_iso, limit))
        else:
            sql = (
                f"SELECT {_SELECT_COLUMNS} FROM background_jobs "
                "ORDER BY created_at DESC LIMIT ?"
            )
            cursor = conn.execute(sql, (limit,))

        for row in cursor.fetchall():
            job = _row_to_tracked_job(row)
            if job.job_id not in seen_ids:
                seen_ids.add(job.job_id)
                result.append(_tracked_job_to_dict(job))

        # Sort by created_at descending (active jobs may not be sorted)
        result.sort(key=lambda d: d.get("created_at") or "", reverse=True)
        return result

    def query_jobs(
        self,
        operation_type: Optional[str] = None,
        status: Optional[str] = None,
        repo_alias: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Query SQLite with optional filters.

        Args:
            operation_type: Filter by operation type.
            status: Filter by status.
            repo_alias: Filter by repo alias.
            limit: Maximum records to return.

        Returns:
            List of job dicts (most-recently-created first).
        """
        where_parts: List[str] = []
        params: List[Any] = []

        if operation_type is not None:
            where_parts.append("operation_type = ?")
            params.append(operation_type)
        if status is not None:
            where_parts.append("status = ?")
            params.append(status)
        if repo_alias is not None:
            where_parts.append("repo_alias = ?")
            params.append(repo_alias)

        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        sql = (
            f"SELECT {_SELECT_COLUMNS} FROM background_jobs "
            f"{where_clause} ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit)

        conn = self._conn_manager.get_connection()
        cursor = conn.execute(sql, params)
        return [_tracked_job_to_dict(_row_to_tracked_job(row)) for row in cursor.fetchall()]

    def check_operation_conflict(
        self,
        operation_type: str,
        repo_alias: Optional[str] = None,
    ) -> None:
        """
        Raise DuplicateJobError if an active or pending job exists for the
        same (operation_type, repo_alias) pair.

        Args:
            operation_type: Operation type to check.
            repo_alias: Repository alias (or None for global operations).

        Raises:
            DuplicateJobError: If a conflicting job is found.
        """
        with self._lock:
            for job in self._active_jobs.values():
                if (
                    job.operation_type == operation_type
                    and job.repo_alias == repo_alias
                    and job.status in ("pending", "running")
                ):
                    raise DuplicateJobError(
                        operation_type=operation_type,
                        repo_alias=repo_alias,
                        existing_job_id=job.job_id,
                    )

    def cleanup_orphaned_jobs_on_startup(self) -> int:
        """
        Mark stale running/pending jobs in SQLite as failed.

        Called once on server startup to handle jobs that were in-flight when
        the server last restarted.  In-memory dict is empty at startup, so
        any job in running/pending state in SQLite is orphaned.

        Returns:
            Number of orphaned jobs marked as failed.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        orphan_error = "orphaned - server restarted"

        def operation(conn) -> int:
            cursor = conn.execute(
                "SELECT job_id FROM background_jobs WHERE status IN ('running', 'pending')"
            )
            orphaned_ids = [row[0] for row in cursor.fetchall()]
            if orphaned_ids:
                conn.execute(
                    "UPDATE background_jobs SET status = 'failed', "
                    "completed_at = ?, error = ? "
                    "WHERE status IN ('running', 'pending')",
                    (now_iso, orphan_error),
                )
            return len(orphaned_ids)

        count: int = self._conn_manager.execute_atomic(operation)
        if count:
            logger.info(
                f"JobTracker.cleanup_orphaned_jobs_on_startup: "
                f"marked {count} orphaned job(s) as failed"
            )
        return count

    def cleanup_old_jobs(
        self, operation_type: str, max_age_hours: int = 24
    ) -> int:
        """
        Remove completed jobs of the given operation_type older than max_age_hours.

        Deletes from SQLite background_jobs table (completed/failed/cancelled status).
        Also removes matching entries from _active_jobs dict if present.

        Args:
            operation_type: Only jobs of this operation type are considered.
            max_age_hours: Jobs with completed_at older than this are deleted.

        Returns:
            Number of jobs deleted from SQLite.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff.isoformat()

        def operation(conn) -> int:
            cursor = conn.execute(
                """DELETE FROM background_jobs
                   WHERE operation_type = ?
                   AND status IN ('completed', 'failed', 'cancelled')
                   AND completed_at IS NOT NULL
                   AND completed_at < ?""",
                (operation_type, cutoff_iso),
            )
            return cursor.rowcount

        count: int = self._conn_manager.execute_atomic(operation)

        # Also evict matching entries from the in-memory dict (edge case guard).
        with self._lock:
            stale_ids = [
                jid
                for jid, j in self._active_jobs.items()
                if j.operation_type == operation_type
                and j.status in ("completed", "failed", "cancelled")
                and j.completed_at is not None
                and j.completed_at < cutoff
            ]
            for jid in stale_ids:
                del self._active_jobs[jid]

        if count > 0:
            logger.info(
                f"JobTracker.cleanup_old_jobs: removed {count} old "
                f"'{operation_type}' job(s) older than {max_age_hours}h"
            )
        return count

    def get_active_job_count(self) -> int:
        """Return the number of in-memory jobs with status 'running'."""
        with self._lock:
            return sum(
                1 for j in self._active_jobs.values() if j.status == "running"
            )

    def get_pending_job_count(self) -> int:
        """Return the number of in-memory jobs with status 'pending'."""
        with self._lock:
            return sum(
                1 for j in self._active_jobs.values() if j.status == "pending"
            )

    # ------------------------------------------------------------------
    # Private SQLite helpers
    # ------------------------------------------------------------------

    def _insert_job(self, job: TrackedJob) -> None:
        """Insert a new job row into background_jobs."""

        def operation(conn) -> None:
            conn.execute(
                """INSERT OR REPLACE INTO background_jobs
                   (job_id, operation_type, status, created_at, started_at,
                    completed_at, result, error, progress, username,
                    is_admin, cancelled, repo_alias, resolution_attempts,
                    progress_info, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, 0, ?, ?)""",
                (
                    job.job_id,
                    job.operation_type,
                    job.status,
                    _dt_to_iso(job.created_at),
                    _dt_to_iso(job.started_at),
                    _dt_to_iso(job.completed_at),
                    json.dumps(job.result) if job.result is not None else None,
                    job.error,
                    job.progress,
                    job.username,
                    job.repo_alias,
                    job.progress_info,
                    json.dumps(job.metadata) if job.metadata is not None else None,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def _upsert_job(self, job: TrackedJob) -> None:
        """Update an existing job row in background_jobs (by job_id)."""

        def operation(conn) -> None:
            conn.execute(
                """UPDATE background_jobs SET
                   status = ?,
                   started_at = ?,
                   completed_at = ?,
                   result = ?,
                   error = ?,
                   progress = ?,
                   progress_info = ?,
                   metadata = ?
                   WHERE job_id = ?""",
                (
                    job.status,
                    _dt_to_iso(job.started_at),
                    _dt_to_iso(job.completed_at),
                    json.dumps(job.result) if job.result is not None else None,
                    job.error,
                    job.progress,
                    job.progress_info,
                    json.dumps(job.metadata) if job.metadata is not None else None,
                    job.job_id,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def _load_job_from_sqlite(self, job_id: str) -> Optional[TrackedJob]:
        """Load a single job from SQLite by job_id."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM background_jobs WHERE job_id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_tracked_job(row)


# ---------------------------------------------------------------------------
# TrackedOperation context manager
# ---------------------------------------------------------------------------


class TrackedOperation:
    """
    Context manager for bounded operations with automatic status transitions.

    On entry:  registers the job (status="pending") and transitions to "running".
    On exit:   if no exception — completes the job (status="completed").
               if exception  — fails the job (status="failed") and re-raises.

    Usage::

        with TrackedOperation(tracker, job_id, "dep_map_analysis", "admin") as job:
            do_work()
            tracker.update_status(job.job_id, progress=50)
    """

    def __init__(
        self,
        tracker: JobTracker,
        job_id: str,
        operation_type: str,
        username: str,
        repo_alias: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tracker = tracker
        self.job_id = job_id
        self.operation_type = operation_type
        self.username = username
        self.repo_alias = repo_alias
        self.metadata = metadata
        self._job: Optional[TrackedJob] = None

    def __enter__(self) -> TrackedJob:
        self._job = self.tracker.register_job(
            self.job_id,
            self.operation_type,
            self.username,
            self.repo_alias,
            self.metadata,
        )
        self.tracker.update_status(self.job_id, status="running")
        assert self._job is not None  # for type checker
        return self._job

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Any,
    ) -> bool:
        if exc_type is not None:
            self.tracker.fail_job(self.job_id, error=str(exc_val))
            return False  # Do not suppress exception
        self.tracker.complete_job(self.job_id)
        return False
