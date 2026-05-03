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
import sqlite3
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
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
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


class _BackendUniqueViolation(Exception):
    """
    Private marker raised by the backend-insert wrapper when the database
    backend signals a unique-index violation (psycopg IntegrityError /
    UniqueViolation). Narrow type so the atomic-gate logic can catch a
    single specific exception without a broad except Exception net.

    Added by Story #876 Phase B-1. Used by the _atomic_insert_impl /
    _atomic_insert_or_raise helpers on JobTracker (added in follow-up edits).
    """


def _require_non_empty_str(name: str, value: Any) -> None:
    """
    Raise ValueError if value is not a non-empty string.

    Module-level primitive validator shared by atomic job registration
    (Story #876 Phase B-1). Callers invoke it per field rather than via a
    higher-level aggregate helper; the single-primitive approach keeps the
    abstraction boundary minimal and consistent with KISS.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got: {value!r}")


def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO-8601 string, or None."""
    if dt is None:
        return None
    return dt.isoformat()


def _serialize_progress_info(value: Optional[Any]) -> Optional[str]:
    """
    Serialize progress_info for SQLite binding.

    SQLite accepts only int, float, str, bytes, and None.  When progress_info
    is a dict (type contract violation or future API change), json.dumps it.
    When it is already a str or None, pass through unchanged.

    Raises:
        TypeError: If the value is a dict containing non-JSON-serializable
            objects (re-raised with a descriptive message), or if the value
            is an unexpected type.  No silent fallback — fail fast
            (Messi Rule #2 anti-fallback).
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        try:
            return json.dumps(value)
        except TypeError as exc:
            raise TypeError(
                f"progress_info dict contains non-JSON-serializable value: {exc}"
            ) from exc
    raise TypeError(
        f"progress_info must be None, str, or dict; got {type(value).__name__}"
    )


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


def _status_priority_sort_key(job_dict: Dict[str, Any]):
    """
    Sort key for get_recent_jobs: running first, then pending/resolving, then completed.

    Story #328: Running jobs must appear above completed jobs regardless of timestamps.
    Within each priority group, most-recently-active timestamp first (descending).
    """
    status = job_dict.get("status", "")
    if status == "running":
        priority = 0
        time_str = job_dict.get("started_at") or job_dict.get("created_at")
    elif status in ("pending", "resolving_prerequisites"):
        priority = 1
        time_str = job_dict.get("created_at")
    else:
        priority = 2
        time_str = job_dict.get("completed_at") or job_dict.get("created_at")

    if time_str:
        dt = datetime.fromisoformat(time_str)
    else:
        dt = datetime.min.replace(tzinfo=timezone.utc)

    return (priority, -dt.timestamp())


# Columns fetched by all SELECT queries — keeps column index mapping single-source.
_SELECT_COLUMNS = (
    "job_id, operation_type, status, created_at, started_at, completed_at, "
    "result, error, progress, username, repo_alias, progress_info, metadata"
)

# Upper bound for backend list_jobs when enumerating candidates for bulk deletion.
_CLEANUP_JOB_FETCH_LIMIT = 10000


# ---------------------------------------------------------------------------
# JobTracker class
# ---------------------------------------------------------------------------


def _dict_to_tracked_job(d: Dict[str, Any]) -> TrackedJob:
    """
    Convert a backend dict (from BackgroundJobsBackend.get_job / list_jobs)
    to a TrackedJob.  Extra keys (is_admin, cancelled, etc.) are ignored.
    """
    return TrackedJob(
        job_id=d["job_id"],
        operation_type=d["operation_type"],
        status=d["status"],
        username=d["username"],
        repo_alias=d.get("repo_alias"),
        progress=d.get("progress") or 0,
        progress_info=d.get("progress_info"),
        metadata=d.get("metadata"),
        created_at=_iso_to_dt(d.get("created_at")) or datetime.now(timezone.utc),
        started_at=_iso_to_dt(d.get("started_at")),
        completed_at=_iso_to_dt(d.get("completed_at")),
        error=d.get("error"),
        result=d.get("result"),
    )


class JobTracker:
    """
    Hybrid job tracker: in-memory dict for active/pending, SQLite for history.

    Thread safety: All mutations to _active_jobs are protected by _lock.
    SQLite I/O is performed outside the lock to avoid holding it during I/O.

    Uses the EXISTING background_jobs table (no new tables).
    New columns progress_info and metadata are added by the schema migration
    _migrate_background_jobs_job_tracker() in DatabaseSchema.

    Story #521: Accepts an optional storage_backend (BackgroundJobsBackend protocol).
    When provided, all DB persistence is delegated to the backend instead of
    direct DatabaseConnectionManager access.
    """

    def __init__(self, db_path: str, storage_backend=None) -> None:
        """
        Initialise tracker.

        Args:
            db_path: Path to the SQLite database file containing background_jobs.
            storage_backend: Optional BackgroundJobsBackend instance.  When
                provided, all DB operations are delegated to this backend
                instead of direct SQLite access.
        """
        self._backend = storage_backend
        if storage_backend is None:
            self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        else:
            self._conn_manager = None  # type: ignore[assignment]
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

    def register_job_if_no_conflict(
        self,
        job_id: str,
        operation_type: str,
        username: str,
        repo_alias: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TrackedJob:
        """
        Atomically register a new pending job unless an active duplicate exists.

        Cluster-atomic replacement for the two-call TOCTOU pattern
        (check_operation_conflict + register_job). The partial unique index
        idx_active_job_per_repo rejects duplicate active jobs at the DB layer,
        so there is no read-then-write race window across cluster nodes.

        repo_alias must be non-empty because the index predicate excludes
        NULL — passing None would silently disable the atomic gate.

        Raises:
            ValueError: If any required string field is None, empty, or
                not a string.
            DuplicateJobError: If an active/pending job already exists for
                the same (operation_type, repo_alias) pair. .existing_job_id
                is populated so callers may join the blocking job.
        """
        _require_non_empty_str("job_id", job_id)
        _require_non_empty_str("operation_type", operation_type)
        _require_non_empty_str("username", username)
        _require_non_empty_str("repo_alias", repo_alias)

        job = TrackedJob(
            job_id=job_id,
            operation_type=operation_type,
            status="pending",
            username=username,
            repo_alias=repo_alias,
            metadata=metadata,
        )

        self._atomic_insert_or_raise(job)

        with self._lock:
            self._active_jobs[job_id] = job

        logger.debug(
            "JobTracker: atomically registered job %s (%s, repo=%s)",
            job_id,
            operation_type,
            repo_alias,
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
                logger.debug(f"JobTracker.update_status: job {job_id} not in memory")
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

    def complete_job(
        self, job_id: str, result: Optional[Dict[str, Any]] = None
    ) -> None:
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
        job.progress = 100
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

    def is_cancelled(self, job_id: str) -> bool:
        """
        Return True if the job's 'cancelled' column is set in the DB.

        Reads the persistence layer DIRECTLY, bypassing the in-memory
        _active_jobs dict.  This is intentional: BackgroundJobManager writes
        cancelled=True to SQLite without touching JobTracker's memory, so only
        a direct DB read can observe the cancellation (Bug #853 Codex Issue 1).

        Args:
            job_id: Job identifier.

        Returns:
            True if the DB row has cancelled=1, False if 0 or row not found.
        """
        if self._backend is not None:
            try:
                job_dict = self._backend.get_job(job_id)
                if job_dict is None:
                    return False
                return bool(job_dict.get("cancelled", False))
            except Exception as e:
                logger.warning(
                    "JobTracker.is_cancelled: backend error for %s: %s", job_id, e
                )
                return False

        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT cancelled FROM background_jobs WHERE job_id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return False
        return bool(row[0])

    def get_active_jobs(self) -> List[TrackedJob]:
        """
        Return a snapshot of all active/pending jobs.

        When a backend is configured, queries the DB for running+pending jobs
        so jobs started by other cluster nodes are included.  In-memory entries
        override DB entries for the same job_id (fresher progress data).

        Returns:
            List of TrackedJob instances (copies, not references).
        """
        if self._backend is not None:
            try:
                db_running = self._backend.list_jobs(status="running")
                db_pending = self._backend.list_jobs(status="pending")
                # Build dict keyed by job_id from DB results (converted to TrackedJob)
                merged: Dict[str, TrackedJob] = {}
                for job_dict in db_running + db_pending:
                    merged[job_dict["job_id"]] = _dict_to_tracked_job(job_dict)
                # Override with in-memory TrackedJob objects (fresher progress)
                with self._lock:
                    for job_id, tracked in self._active_jobs.items():
                        if tracked.status in ("running", "pending"):
                            merged[job_id] = tracked
                return list(merged.values())
            except Exception as e:
                logger.warning(
                    "Failed to query backend for active jobs, falling back to in-memory: %s",
                    e,
                )
        with self._lock:
            return list(self._active_jobs.values())

    def get_recent_jobs(
        self,
        limit: int = 20,
        time_filter: str = "24h",
    ) -> List[Dict[str, Any]]:
        """
        Merge active in-memory jobs + recent historical jobs from the store.

        Active jobs are always included regardless of the time filter.
        Historical jobs are filtered by created_at within the time window.

        Args:
            limit: Maximum number of historical records from the store.
            time_filter: "1h", "24h", "7d", "30d", or "all".

        Returns:
            List of job dicts (most-recently-created first), deduped by job_id.
        """
        # Collect active jobs first (always included)
        with self._lock:
            active_snapshot = list(self._active_jobs.values())

        seen_ids = {j.job_id for j in active_snapshot}
        result = [_tracked_job_to_dict(j) for j in active_snapshot]

        # Compute cutoff ISO string
        cutoff_iso: Optional[str] = None
        if time_filter != "all":
            delta_map = {"1h": 1, "24h": 24, "7d": 168, "30d": 720}
            hours = delta_map.get(time_filter, 24)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cutoff_iso = cutoff.isoformat()

        if self._backend is not None:
            # Fetch from backend and filter client-side by cutoff
            historical = self._backend.list_jobs(limit=limit)
            for job_dict in historical:
                if job_dict["job_id"] in seen_ids:
                    continue
                if cutoff_iso and (job_dict.get("created_at") or "") < cutoff_iso:
                    continue
                seen_ids.add(job_dict["job_id"])
                result.append(job_dict)
            result.sort(key=_status_priority_sort_key)
            return result

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

        # Story #328: Sort with running/pending jobs first, then by time
        result.sort(key=_status_priority_sort_key)
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
        if self._backend is not None:
            rows: List[Dict[str, Any]] = self._backend.list_jobs(
                operation_type=operation_type,
                status=status,
                limit=limit,
            )
            # Apply repo_alias filter client-side (list_jobs has no repo_alias param)
            if repo_alias is not None:
                rows = [r for r in rows if r.get("repo_alias") == repo_alias]
            return rows

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
        return [
            _tracked_job_to_dict(_row_to_tracked_job(row)) for row in cursor.fetchall()
        ]

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
        if self._backend is not None:
            try:
                running_jobs = self._backend.list_jobs(
                    status="running", operation_type=operation_type
                )
                pending_jobs = self._backend.list_jobs(
                    status="pending", operation_type=operation_type
                )
                for job_dict in running_jobs + pending_jobs:
                    if job_dict.get("repo_alias") == repo_alias:
                        raise DuplicateJobError(
                            operation_type=operation_type,
                            repo_alias=repo_alias,
                            existing_job_id=job_dict.get("job_id", "unknown"),
                        )
                return
            except DuplicateJobError:
                raise
            except Exception as e:
                logger.warning(
                    "Failed to check operation conflict from backend, falling back to in-memory: %s",
                    e,
                )

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
        Mark stale running/pending jobs as failed.

        Called once on server startup to handle jobs that were in-flight when
        the server last restarted.  In-memory dict is empty at startup, so
        any job in running/pending state in the store is orphaned.

        Returns:
            Number of orphaned jobs marked as failed.
        """
        if self._backend is not None:
            count: int = int(self._backend.cleanup_orphaned_jobs_on_startup())
            if count:
                logger.info(
                    f"JobTracker.cleanup_orphaned_jobs_on_startup: "
                    f"marked {count} orphaned job(s) as failed"
                )
            return count

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

        sqlite_count: int = int(self._conn_manager.execute_atomic(operation))
        if sqlite_count:
            logger.info(
                f"JobTracker.cleanup_orphaned_jobs_on_startup: "
                f"marked {sqlite_count} orphaned job(s) as failed"
            )
        return sqlite_count

    def _evict_stale_from_memory(self, operation_type: str, cutoff: datetime) -> None:
        """Remove stale completed/failed/cancelled jobs from in-memory dict."""
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

    def cleanup_old_jobs(self, operation_type: str, max_age_hours: int = 24) -> int:
        """
        Remove completed jobs of the given operation_type older than max_age_hours.

        Deletes from the persistence store (completed/failed/cancelled status).
        Also removes matching entries from _active_jobs dict if present.

        Args:
            operation_type: Only jobs of this operation type are considered.
            max_age_hours: Jobs with completed_at older than this are deleted.

        Returns:
            Number of jobs deleted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cutoff_iso = cutoff.isoformat()

        if self._backend is not None:
            # Backend cleanup_old_jobs has no operation_type filter, so we
            # enumerate candidates per terminal status and delete individually.
            count = 0
            for terminal_status in ("completed", "failed", "cancelled"):
                candidates = self._backend.list_jobs(
                    operation_type=operation_type,
                    status=terminal_status,
                    limit=_CLEANUP_JOB_FETCH_LIMIT,
                )
                for job_dict in candidates:
                    completed_at_str = job_dict.get("completed_at")
                    if completed_at_str and completed_at_str < cutoff_iso:
                        self._backend.delete_job(job_dict["job_id"])
                        count += 1

            self._evict_stale_from_memory(operation_type, cutoff)
            if count > 0:
                logger.info(
                    f"JobTracker.cleanup_old_jobs: removed {count} old "
                    f"'{operation_type}' job(s) older than {max_age_hours}h"
                )
            return count

        def operation(conn) -> int:
            cursor = conn.execute(
                """DELETE FROM background_jobs
                   WHERE operation_type = ?
                   AND status IN ('completed', 'failed', 'cancelled')
                   AND completed_at IS NOT NULL
                   AND completed_at < ?""",
                (operation_type, cutoff_iso),
            )
            return cursor.rowcount  # type: ignore[no-any-return]

        sqlite_count: int = int(self._conn_manager.execute_atomic(operation))  # type: ignore[arg-type]
        self._evict_stale_from_memory(operation_type, cutoff)

        if sqlite_count > 0:
            logger.info(
                f"JobTracker.cleanup_old_jobs: removed {sqlite_count} old "
                f"'{operation_type}' job(s) older than {max_age_hours}h"
            )
        return sqlite_count

    def get_active_job_count(self) -> int:
        """Return the number of jobs with status 'running' (DB-first for cluster correctness)."""
        if self._backend is not None:
            try:
                counts = self._backend.count_jobs_by_status()
                return counts.get("running", 0)  # type: ignore[no-any-return]
            except Exception as e:
                logger.warning(
                    "Failed to get active job count from backend, falling back to in-memory: %s",
                    e,
                )
        with self._lock:
            return sum(1 for j in self._active_jobs.values() if j.status == "running")

    def get_pending_job_count(self) -> int:
        """Return the number of jobs with status 'pending' (DB-first for cluster correctness)."""
        if self._backend is not None:
            try:
                counts = self._backend.count_jobs_by_status()
                return counts.get("pending", 0)  # type: ignore[no-any-return]
            except Exception as e:
                logger.warning(
                    "Failed to get pending job count from backend, falling back to in-memory: %s",
                    e,
                )
        with self._lock:
            return sum(1 for j in self._active_jobs.values() if j.status == "pending")

    def get_running_jobs_count(self) -> int:
        """Return the count of running jobs.

        Alias for get_active_job_count() using the plural-form name expected by
        MaintenanceState.register_job_tracker() interface (drain-status monitoring).
        """
        return self.get_active_job_count()

    def get_queued_jobs_count(self) -> int:
        """Return the count of pending (queued) jobs.

        Alias for get_pending_job_count() using the plural-form name expected by
        MaintenanceState.register_job_tracker() interface (drain-status monitoring).
        """
        return self.get_pending_job_count()

    # ------------------------------------------------------------------
    # Private SQLite helpers
    # ------------------------------------------------------------------

    def _atomic_insert_or_raise(self, job: TrackedJob) -> None:
        """
        Insert a job row atomically, translating a unique-index violation on
        idx_active_job_per_repo (Story #876 Phase C) into DuplicateJobError.

        Precondition: job.repo_alias must be non-null (the partial index
        predicate excludes NULL — callers guard via _require_non_empty_str).

        SQLite path catches sqlite3.IntegrityError directly.
        Backend path catches the narrow _BackendUniqueViolation marker that
        _atomic_insert_impl raises in place of psycopg's IntegrityError.

        If the lookup for the blocking row returns None, raises RuntimeError
        — treating an inconsistent database state as a hard error rather
        than substituting a fallback value (Messi Rule #2 anti-fallback).
        """
        assert job.repo_alias is not None, (
            "atomic insert requires non-null repo_alias — partial index excludes NULL"
        )
        try:
            self._atomic_insert_impl(job)
            return
        except (sqlite3.IntegrityError, _BackendUniqueViolation):
            pass

        existing_id = self._find_blocking_active_job_id(
            job.operation_type, job.repo_alias
        )
        if existing_id is None:
            raise RuntimeError(
                f"atomic insert raised IntegrityError for "
                f"({job.operation_type}, {job.repo_alias}) but no active row "
                f"was found in the lookup; database state is inconsistent"
            )
        raise DuplicateJobError(
            operation_type=job.operation_type,
            repo_alias=job.repo_alias,
            existing_job_id=existing_id,
        )

    def _atomic_insert_impl(self, job: TrackedJob) -> None:
        """
        Raw INSERT that surfaces partial-unique-index violations.

        Avoids INSERT OR IGNORE / OR REPLACE so sqlite3.IntegrityError is
        raised on duplicates. On the backend path, wraps save_job() in a
        narrow try/except that translates psycopg's IntegrityError /
        UniqueViolation into the _BackendUniqueViolation marker without
        importing psycopg. All other exceptions are re-raised.
        """
        if self._backend is not None:
            try:
                self._backend.save_job(
                    job_id=job.job_id,
                    operation_type=job.operation_type,
                    status=job.status,
                    created_at=_dt_to_iso(job.created_at) or "",
                    username=job.username,
                    progress=job.progress,
                    started_at=_dt_to_iso(job.started_at),
                    completed_at=_dt_to_iso(job.completed_at),
                    result=job.result,
                    error=job.error,
                    repo_alias=job.repo_alias,
                    progress_info=job.progress_info,
                    metadata=job.metadata,
                )
            except Exception as exc:
                # Narrow detection: translate only the known unique-violation
                # shapes into the marker; re-raise anything else untouched.
                if type(exc).__name__ in ("IntegrityError", "UniqueViolation"):
                    raise _BackendUniqueViolation(str(exc)) from exc
                raise
            return

        def operation(conn) -> None:
            conn.execute(
                """INSERT INTO background_jobs
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
                    _serialize_progress_info(job.progress_info),
                    json.dumps(job.metadata) if job.metadata is not None else None,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def _find_blocking_active_job_id(
        self, operation_type: str, repo_alias: str
    ) -> Optional[str]:
        """
        Return the job_id of the active (pending or running) row currently
        blocking a duplicate INSERT for (operation_type, repo_alias).

        Called only after a unique-index violation on
        idx_active_job_per_repo, so the caller can populate
        DuplicateJobError.existing_job_id.

        Returns None if no blocking row is visible — the caller treats that
        as an inconsistent database state (row vanished between INSERT and
        lookup) and raises RuntimeError rather than substituting a fallback.
        """
        if self._backend is not None:
            for status in ("running", "pending"):
                rows = self._backend.list_jobs(
                    status=status, operation_type=operation_type
                )
                for row in rows:
                    if row.get("repo_alias") == repo_alias:
                        # Explicit type narrowing: list_jobs returns
                        # List[Dict[str, Any]], so row.get("job_id") is Any.
                        # A well-formed row has a string job_id; guard
                        # defensively rather than suppress mypy.
                        candidate = row.get("job_id")
                        return candidate if isinstance(candidate, str) else None
            return None

        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT job_id FROM background_jobs "
            "WHERE operation_type = ? AND repo_alias = ? "
            "AND status IN ('pending', 'running') LIMIT 1",
            (operation_type, repo_alias),
        )
        row = cursor.fetchone()
        return row[0] if row is not None else None

    def _insert_job(self, job: TrackedJob) -> None:
        """Insert a new job row into background_jobs."""
        if self._backend is not None:
            self._backend.save_job(
                job_id=job.job_id,
                operation_type=job.operation_type,
                status=job.status,
                created_at=_dt_to_iso(job.created_at) or "",
                username=job.username,
                progress=job.progress,
                started_at=_dt_to_iso(job.started_at),
                completed_at=_dt_to_iso(job.completed_at),
                result=job.result,
                error=job.error,
                repo_alias=job.repo_alias,
                progress_info=job.progress_info,
                metadata=job.metadata,
            )
            return

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
                    _serialize_progress_info(job.progress_info),
                    json.dumps(job.metadata) if job.metadata is not None else None,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def _upsert_job(self, job: TrackedJob) -> None:
        """Update an existing job row in background_jobs (by job_id)."""
        if self._backend is not None:
            self._backend.update_job(
                job.job_id,
                status=job.status,
                started_at=_dt_to_iso(job.started_at),
                completed_at=_dt_to_iso(job.completed_at),
                result=job.result,
                error=job.error,
                progress=job.progress,
                progress_info=job.progress_info,
                metadata=job.metadata,
            )
            return

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
                    _serialize_progress_info(job.progress_info),
                    json.dumps(job.metadata) if job.metadata is not None else None,
                    job.job_id,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def _load_job_from_sqlite(self, job_id: str) -> Optional[TrackedJob]:
        """Load a single job from SQLite (or backend) by job_id."""
        if self._backend is not None:
            d = self._backend.get_job(job_id)
            if d is None:
                return None
            return _dict_to_tracked_job(d)

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
    ) -> None:
        if exc_type is not None:
            self.tracker.fail_job(self.job_id, error=str(exc_val))
            return  # Do not suppress exception
        self.tracker.complete_job(self.job_id)
        return  # implicit None — do not suppress exceptions
