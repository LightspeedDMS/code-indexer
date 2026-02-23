"""
Background Job System for CIDX Server.

Manages asynchronous operations for golden repositories and other long-running tasks.
Provides persistence, user isolation, job management, and comprehensive tracking.
"""

import json
import logging
import queue
import threading
import uuid
import inspect
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, Any, Optional, Callable, TYPE_CHECKING, List
from dataclasses import dataclass, asdict

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import (
        ServerResourceConfig,
        BackgroundJobsConfig,
    )
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend


class JobStatus(str, Enum):
    """Job status enumeration."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RESOLVING_PREREQUISITES = "resolving_prerequisites"  # AC2: SCIP self-healing state


class DuplicateJobError(Exception):
    """Raised when attempting to submit a duplicate job (Bug #133)."""

    def __init__(self, operation_type: str, repo_alias: str, existing_job_id: str):
        self.operation_type = operation_type
        self.repo_alias = repo_alias
        self.existing_job_id = existing_job_id
        super().__init__(
            f"A '{operation_type}' job is already running for repository '{repo_alias}' "
            f"(job_id: {existing_job_id}). Please wait for it to complete."
        )


@dataclass
class BackgroundJob:
    """Background job data structure with SCIP self-healing support."""

    job_id: str
    operation_type: str
    status: JobStatus
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    progress: int  # 0-100
    username: str  # User who submitted the job
    is_admin: bool = False  # Admin priority flag
    cancelled: bool = False  # Cancellation flag

    # SCIP Self-Healing Fields (AC1: Extended BackgroundJob Model)
    repo_alias: Optional[str] = None  # Repository being processed
    resolution_attempts: int = 0  # Total Claude Code invocations across all projects
    claude_actions: Optional[List[str]] = None  # Aggregated actions from all projects
    failure_reason: Optional[str] = None  # Human-readable failure explanation
    extended_error: Optional[Dict[str, Any]] = None  # Structured error context
    language_resolution_status: Optional[Dict[str, Dict[str, Any]]] = (
        None  # Per-project tracking
    )


class BackgroundJobManager:
    """
    Enhanced background job manager for long-running operations.

    Provides job queuing, execution, status tracking, persistence,
    user isolation, and comprehensive job management functionality.
    """

    def __init__(
        self,
        storage_path: Optional[str] = None,
        resource_config: Optional["ServerResourceConfig"] = None,
        use_sqlite: bool = False,
        db_path: Optional[str] = None,
        background_jobs_config: Optional["BackgroundJobsConfig"] = None,
    ):
        """Initialize enhanced background job manager.

        Args:
            storage_path: Path for persistent job storage (JSON file, optional)
            resource_config: Resource configuration (limits, timeouts)
            use_sqlite: Whether to use SQLite backend instead of JSON file
            db_path: Path to SQLite database file (required if use_sqlite=True)
            background_jobs_config: Background jobs configuration (concurrency limits)
        """
        self.jobs: Dict[str, BackgroundJob] = {}
        self._lock = threading.Lock()
        self._executor = None
        self._running_jobs: Dict[str, threading.Thread] = {}
        self._job_queue: queue.PriorityQueue = queue.PriorityQueue()
        # Story #26: Queue for pending jobs waiting for a slot
        self._pending_job_queue: queue.Queue = queue.Queue()

        # Persistence settings
        self.storage_path = storage_path
        self.use_sqlite = use_sqlite
        self.db_path = db_path
        self._sqlite_backend: Optional["BackgroundJobsSqliteBackend"] = None

        # Initialize SQLite backend if enabled
        if self.use_sqlite and self.db_path:
            from code_indexer.server.storage.sqlite_backends import (
                BackgroundJobsSqliteBackend,
            )

            self._sqlite_backend = BackgroundJobsSqliteBackend(self.db_path)
            logging.info("BackgroundJobManager using SQLite backend")

        # Resource configuration (import here to avoid circular dependency)
        if resource_config is None:
            from code_indexer.server.utils.config_manager import ServerResourceConfig

            resource_config = ServerResourceConfig()
        self.resource_config = resource_config

        # Story #26: Background jobs configuration (concurrency limits)
        if background_jobs_config is None:
            from code_indexer.server.utils.config_manager import BackgroundJobsConfig

            background_jobs_config = BackgroundJobsConfig()
        self._background_jobs_config = background_jobs_config

        # Story #26: Semaphore for limiting concurrent job execution
        self._job_semaphore = threading.Semaphore(
            self._background_jobs_config.max_concurrent_background_jobs
        )

        # Load persisted jobs
        self._load_jobs()

        # Background job manager initialized silently

    @property
    def max_concurrent_jobs(self) -> int:
        """Get the maximum number of concurrent background jobs (Story #26)."""
        return self._background_jobs_config.max_concurrent_background_jobs

    def _check_operation_conflict(
        self, operation_type: str, repo_alias: str
    ) -> Optional[str]:
        """Check if operation is already running for this repository.

        Bug #133: Prevent duplicate jobs with same (operation_type, repo_alias)
        from running concurrently.

        Returns job_id of conflicting job, or None if no conflict.
        Must be called while holding self._lock.

        Args:
            operation_type: Type of operation (e.g., 'refresh_golden_repo')
            repo_alias: Repository alias being processed

        Returns:
            Job ID of conflicting job if found, None otherwise
        """
        for job in self.jobs.values():
            if (
                job.operation_type == operation_type
                and job.repo_alias == repo_alias
                and job.status in (JobStatus.PENDING, JobStatus.RUNNING)
            ):
                return job.job_id
        return None

    def submit_job(
        self,
        operation_type: str,
        func: Callable[[], Dict[str, Any]],
        *args,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias: Optional[str] = None,  # AC5: Fix unknown repo bug
        **kwargs,
    ) -> str:
        """
        Submit a job for background execution.

        Args:
            operation_type: Type of operation (e.g., 'add_golden_repo')
            func: Function to execute
            *args: Function arguments
            submitter_username: Username of the job submitter
            is_admin: Whether this is an admin job (higher priority)
            repo_alias: Repository alias being processed (AC5: Fix unknown repo bug)
            **kwargs: Function keyword arguments

        Returns:
            Job ID for tracking

        Raises:
            Exception: If user has exceeded max jobs limit (if configured)
        """
        # Check maintenance mode first (Story #734)
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )
        from code_indexer.server.jobs.exceptions import MaintenanceModeError

        if get_maintenance_state().is_maintenance_mode():
            raise MaintenanceModeError()

        # NOTE: max_jobs_per_user limit has been removed as an artificial constraint
        # Jobs are no longer limited per user

        # AC5: Validate repo_alias to prevent "unknown" values
        if repo_alias is None:
            logging.warning(
                f"Job submitted without repo_alias for operation '{operation_type}' "
                f"by user '{submitter_username}'. Consider providing repo_alias."
            )
        elif repo_alias.lower() == "unknown":
            logging.warning(
                f"Job submitted with repo_alias='unknown' for operation '{operation_type}' "
                f"by user '{submitter_username}'. This may indicate missing repository context."
            )

        job_id = str(uuid.uuid4())

        job = BackgroundJob(
            job_id=job_id,
            operation_type=operation_type,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username=submitter_username,
            is_admin=is_admin,
            repo_alias=repo_alias,  # AC5: Store repo_alias
        )

        with self._lock:
            # Bug #133: Check for duplicate operation on same repo
            # This check MUST be inside the lock to prevent TOCTOU race conditions
            if repo_alias:
                conflict_job_id = self._check_operation_conflict(
                    operation_type, repo_alias
                )
                if conflict_job_id:
                    raise DuplicateJobError(operation_type, repo_alias, conflict_job_id)

            self.jobs[job_id] = job

        # Story #267 Component 3-4: Persist outside lock
        self._persist_jobs(job_id=job_id)

        # Execute job in background thread
        thread = threading.Thread(
            target=self._execute_job, args=(job_id, func, args, kwargs)
        )
        # Thread is not daemon to ensure proper shutdown
        thread.start()

        # Track running thread
        with self._lock:
            self._running_jobs[job_id] = thread

        logging.info(
            f"Background job {job_id} submitted by {submitter_username}: {operation_type}"
        )
        return job_id

    def get_job_status(self, job_id: str, username: str) -> Optional[Dict[str, Any]]:
        """
        Get status of a background job with user isolation.

        Story #267 Component 8: Falls back to SQLite when job is not in memory,
        since completed/failed jobs are removed from memory after persistence.

        Args:
            job_id: Job ID to check
            username: Username requesting the status (for authorization)

        Returns:
            Job status dictionary or None if job not found or not authorized
        """
        with self._lock:
            job = self.jobs.get(job_id)
            if job:
                if job.username != username:
                    return None
                return {
                    "job_id": job.job_id,
                    "operation_type": job.operation_type,
                    "status": job.status.value,
                    "created_at": job.created_at.isoformat(),
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                    "completed_at": (
                        job.completed_at.isoformat() if job.completed_at else None
                    ),
                    "progress": job.progress,
                    "result": job.result,
                    "error": job.error,
                    "username": job.username,
                    "repo_alias": job.repo_alias,  # AC5: Include repo_alias in response
                    # AC6: Extended self-healing fields
                    "resolution_attempts": job.resolution_attempts,
                    "claude_actions": job.claude_actions,
                    "failure_reason": job.failure_reason,
                    "extended_error": job.extended_error,
                    "language_resolution_status": job.language_resolution_status,
                }

        # Story #267 Component 8: Fall back to SQLite for completed/failed jobs
        if self._sqlite_backend:
            try:
                db_job = self._sqlite_backend.get_job(job_id)
                if db_job and db_job.get("username") == username:
                    return db_job
            except Exception as e:
                logging.error(f"Failed to get job {job_id} from SQLite: {e}")

        return None

    def list_jobs(
        self,
        username: str,
        status_filter: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        List jobs for a user with filtering and pagination.

        Args:
            username: Username to filter jobs for
            status_filter: Optional status filter
            limit: Maximum number of jobs to return
            offset: Number of jobs to skip

        Returns:
            Dictionary with jobs list and total count
        """
        with self._lock:
            # Filter jobs by user
            user_jobs = [job for job in self.jobs.values() if job.username == username]

            # Apply status filter if provided
            if status_filter:
                user_jobs = [
                    job for job in user_jobs if job.status.value == status_filter
                ]

            # Sort by creation time (newest first)
            user_jobs.sort(key=lambda x: x.created_at, reverse=True)

            total_count = len(user_jobs)

            # Apply pagination
            paginated_jobs = user_jobs[offset : offset + limit]

            # Convert to dictionary format
            job_dicts = []
            for job in paginated_jobs:
                job_dicts.append(
                    {
                        "job_id": job.job_id,
                        "operation_type": job.operation_type,
                        "status": job.status.value,
                        "created_at": job.created_at.isoformat(),
                        "started_at": (
                            job.started_at.isoformat() if job.started_at else None
                        ),
                        "completed_at": (
                            job.completed_at.isoformat() if job.completed_at else None
                        ),
                        "progress": job.progress,
                        "result": job.result,
                        "error": job.error,
                        "username": job.username,
                        "repo_alias": job.repo_alias,  # AC5: Include repo_alias in list
                        # AC6: Extended self-healing fields
                        "resolution_attempts": job.resolution_attempts,
                        "claude_actions": job.claude_actions,
                        "failure_reason": job.failure_reason,
                        "extended_error": job.extended_error,
                        "language_resolution_status": job.language_resolution_status,
                    }
                )

            return {
                "jobs": job_dicts,
                "total": total_count,
                "limit": limit,
                "offset": offset,
            }

    def cancel_job(self, job_id: str, username: str) -> Dict[str, Any]:
        """
        Cancel a running or pending job.

        Args:
            job_id: Job ID to cancel
            username: Username requesting cancellation (for authorization)

        Returns:
            Cancellation result dictionary
        """
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or job.username != username:
                return {"success": False, "message": "Job not found or not authorized"}

            if job.status not in [JobStatus.PENDING, JobStatus.RUNNING]:
                return {
                    "success": False,
                    "message": f"Cannot cancel job in {job.status.value} status",
                }

            # Mark job as cancelled
            job.cancelled = True

            if job.status == JobStatus.PENDING:
                # If pending, immediately mark as cancelled
                job.status = JobStatus.CANCELLED
                job.completed_at = datetime.now(timezone.utc)
            elif job.status == JobStatus.RUNNING:
                # For running jobs, the job execution will detect cancellation
                # and update status accordingly
                pass

        # Story #267 Component 3-4: Persist outside lock
        self._persist_jobs(job_id=job_id)

        logging.info(f"Job {job_id} cancelled by user {username}")
        return {"success": True, "message": "Job cancelled successfully"}

    def _execute_job(
        self, job_id: str, func: Callable[[], Dict[str, Any]], args: tuple, kwargs: dict
    ) -> None:
        """
        Execute a background job with cancellation support and concurrency limiting.

        Story #26: Jobs wait for a semaphore slot before transitioning to RUNNING.
        Jobs stay in PENDING state until a slot is available.

        Args:
            job_id: Job ID
            func: Function to execute
            args: Function arguments
            kwargs: Function keyword arguments
        """
        # Story #26: Wait for a slot in the semaphore (blocks if limit reached)
        # Job remains in PENDING state while waiting
        logging.debug(
            f"Job {job_id} waiting for execution slot (current limit: {self.max_concurrent_jobs})"
        )
        self._job_semaphore.acquire()

        try:
            # Now we have a slot - check if job was cancelled while waiting
            with self._lock:
                job = self.jobs[job_id]
                if job.cancelled:
                    job.status = JobStatus.CANCELLED
                    job.completed_at = datetime.now(timezone.utc)
            # Story #267 Component 3-4: Persist outside lock
            if job.cancelled:
                self._persist_jobs(job_id=job_id)
                return

            with self._lock:
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(timezone.utc)
                job.progress = 10
            # Story #267 Component 3-4: Persist outside lock
            self._persist_jobs(job_id=job_id)

            logging.info(f"Starting background job {job_id}")

            # Create progress callback function
            def progress_callback(progress: int):
                with self._lock:
                    if job_id in self.jobs and not self.jobs[job_id].cancelled:
                        self.jobs[job_id].progress = progress
                # Story #267 Component 3-4: Persist outside lock
                self._persist_jobs(job_id=job_id)

            # Check if function accepts progress callback
            func_signature = inspect.signature(func)

            # Update progress during execution
            progress_callback(25)

            # Check for cancellation before execution
            cancelled = False
            with self._lock:
                if self.jobs[job_id].cancelled:
                    self.jobs[job_id].status = JobStatus.CANCELLED
                    self.jobs[job_id].completed_at = datetime.now(timezone.utc)
                    cancelled = True
            if cancelled:
                # Story #267 Component 3-4: Persist outside lock
                self._persist_jobs(job_id=job_id)
                return

            # Execute the actual operation with frequent cancellation checks
            if "progress_callback" in func_signature.parameters:
                # Add progress_callback to kwargs
                enhanced_kwargs = kwargs.copy()
                enhanced_kwargs["progress_callback"] = progress_callback
                result = func(*args, **enhanced_kwargs)
            else:
                # For functions without progress callback, we need to wrap execution
                # to check for cancellation periodically
                result = self._execute_with_cancellation_check(
                    job_id, func, args, kwargs
                )

            # Job completed successfully
            with self._lock:
                job = self.jobs[job_id]
                if not job.cancelled:
                    job.status = JobStatus.COMPLETED
                    job.completed_at = datetime.now(timezone.utc)
                    job.result = result
                    job.progress = 100
                else:
                    job.status = JobStatus.CANCELLED
                    job.completed_at = datetime.now(timezone.utc)
            # Story #267 Component 3-4: Persist outside lock
            self._persist_jobs(job_id=job_id)
            # Story #267 Component 8: Remove completed/cancelled jobs from memory
            # Only when SQLite backend is available (data is preserved in DB)
            if self._sqlite_backend and job.status in (
                JobStatus.COMPLETED, JobStatus.CANCELLED
            ):
                with self._lock:
                    self.jobs.pop(job_id, None)

            logging.info(f"Background job {job_id} completed successfully")

        except InterruptedError as e:
            # Job was cancelled
            logging.info(f"Background job {job_id} was cancelled: {e}")
            with self._lock:
                job = self.jobs[job_id]
                job.status = JobStatus.CANCELLED
                job.completed_at = datetime.now(timezone.utc)
                job.error = str(e)
                job.progress = 0
            # Story #267 Component 3-4: Persist outside lock
            self._persist_jobs(job_id=job_id)
            # Story #267 Component 8: Remove from memory after persist (SQLite only)
            if self._sqlite_backend:
                with self._lock:
                    self.jobs.pop(job_id, None)
        except Exception as e:
            # Job failed
            error_msg = str(e)
            logging.error(f"Background job {job_id} failed: {error_msg}")

            with self._lock:
                job = self.jobs[job_id]
                job.status = JobStatus.FAILED
                job.completed_at = datetime.now(timezone.utc)
                job.error = error_msg
                job.progress = 0
            # Story #267 Component 3-4: Persist outside lock
            self._persist_jobs(job_id=job_id)
            # Story #267 Component 8: Remove from memory after persist (SQLite only)
            if self._sqlite_backend:
                with self._lock:
                    self.jobs.pop(job_id, None)

        finally:
            # Story #26: Release semaphore slot to allow another job to run
            self._job_semaphore.release()
            # Clean up running job reference
            with self._lock:
                self._running_jobs.pop(job_id, None)

    def _execute_with_cancellation_check(
        self, job_id: str, func: Callable, args: tuple, kwargs: dict
    ) -> Any:
        """
        Execute a function with periodic cancellation checks.

        For long-running functions that don't support progress callbacks,
        this method runs them in a separate thread and checks for cancellation.
        """
        import threading
        import queue
        from typing import Any

        result_queue: queue.Queue[Any] = queue.Queue()
        exception_queue: queue.Queue[Exception] = queue.Queue()

        def worker():
            try:
                result = func(*args, **kwargs)
                result_queue.put(result)
            except Exception as e:
                exception_queue.put(e)

        # Start function in separate thread
        worker_thread = threading.Thread(target=worker)
        # Worker thread is not daemon to ensure proper shutdown
        worker_thread.start()

        # Poll for completion or cancellation
        while worker_thread.is_alive():
            # Check for cancellation
            with self._lock:
                if self.jobs[job_id].cancelled:
                    # Function is still running, but we mark as cancelled
                    # The thread will continue but we ignore its result
                    raise InterruptedError("Job cancelled during execution")

            # Wait a bit before next check
            worker_thread.join(timeout=0.1)

        # Check if there was an exception
        if not exception_queue.empty():
            raise exception_queue.get()

        # Return result if available
        if not result_queue.empty():
            return result_queue.get()

        # Should not reach here normally
        return {"status": "completed"}

    def cleanup_old_jobs(self, max_age_hours: int = 24) -> int:
        """
        Clean up old completed/failed jobs from both memory and SQLite.

        Story #267 Component 5: Wire to sqlite_backend.cleanup_old_jobs().

        Args:
            max_age_hours: Maximum age of jobs to keep in hours

        Returns:
            Number of jobs cleaned up
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cleaned_count = 0

        # Step 1: Clean in-memory (under lock)
        with self._lock:
            job_ids_to_remove = []

            for job_id, job in self.jobs.items():
                if (
                    job.status
                    in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
                    and job.completed_at
                    and job.completed_at < cutoff_time
                ):
                    job_ids_to_remove.append(job_id)

            for job_id in job_ids_to_remove:
                del self.jobs[job_id]
                cleaned_count += 1

        # Step 2: Clean SQLite (outside lock) - Story #267 Component 5
        if self._sqlite_backend:
            try:
                db_cleaned = self._sqlite_backend.cleanup_old_jobs(max_age_hours)
                cleaned_count = max(cleaned_count, db_cleaned)
            except Exception as e:
                logging.error(f"Failed to cleanup old jobs from SQLite: {e}")

        if cleaned_count > 0:
            logging.info(f"Cleaned up {cleaned_count} old background jobs")

        return cleaned_count

    def get_active_job_count(self) -> int:
        """
        Get count of currently active/running jobs.

        Returns:
            Number of active jobs
        """
        with self._lock:
            return sum(
                1 for job in self.jobs.values() if job.status == JobStatus.RUNNING
            )

    def get_pending_job_count(self) -> int:
        """
        Get count of pending jobs waiting to be executed.

        Returns:
            Number of pending jobs
        """
        with self._lock:
            return sum(
                1 for job in self.jobs.values() if job.status == JobStatus.PENDING
            )

    def get_failed_job_count(self) -> int:
        """
        Get count of failed jobs.

        Story #267 Component 8: Query SQLite for failed count since failed jobs
        are removed from memory after persistence.

        Returns:
            Number of failed jobs
        """
        # Story #267 Component 8: Use SQLite for failed count
        if self._sqlite_backend:
            try:
                status_counts = self._sqlite_backend.count_jobs_by_status()
                return status_counts.get("failed", 0)
            except Exception as e:
                logging.error(f"Failed to get failed job count from SQLite: {e}")
                return 0

        # Fallback: iterate in-memory jobs (for JSON storage or no backend)
        with self._lock:
            return sum(
                1 for job in self.jobs.values() if job.status == JobStatus.FAILED
            )

    def get_running_job_count(self) -> int:
        """
        Get count of currently running jobs (Story #26).

        This is an alias for get_active_job_count for consistency with
        the concurrency limiting feature naming.

        Returns:
            Number of running jobs
        """
        return self.get_active_job_count()

    def get_queued_job_count(self) -> int:
        """
        Get count of jobs waiting in queue for a slot (Story #26).

        This is an alias for get_pending_job_count for consistency with
        the concurrency limiting feature naming.

        Returns:
            Number of pending/queued jobs
        """
        return self.get_pending_job_count()

    def get_job_queue_metrics(self) -> Dict[str, int]:
        """
        Get combined job queue metrics (Story #26).

        Returns:
            Dictionary with running_count, queued_count, and max_concurrent
        """
        with self._lock:
            running_count = sum(
                1 for job in self.jobs.values() if job.status == JobStatus.RUNNING
            )
            queued_count = sum(
                1 for job in self.jobs.values() if job.status == JobStatus.PENDING
            )

        return {
            "running_count": running_count,
            "queued_count": queued_count,
            "max_concurrent": self.max_concurrent_jobs,
        }

    def shutdown(self) -> None:
        """
        Graceful shutdown of all running jobs.

        Cancels all running jobs and waits for threads to complete.
        This method should be called during application shutdown.
        """
        with self._lock:
            # Cancel all running jobs
            running_job_ids = list(self._running_jobs.keys())
            for job_id in running_job_ids:
                job = self.jobs.get(job_id)
                if job and job.status == JobStatus.RUNNING:
                    job.cancelled = True
                    job.status = JobStatus.CANCELLED
                    job.completed_at = datetime.now(timezone.utc)
                    logging.info(f"Job {job_id} cancelled during shutdown")

            # Snapshot jobs under lock for persistence
            jobs_snapshot = {
                jid: self._snapshot_job(j) for jid, j in self.jobs.items()
            }

            # Get list of threads to wait for
            threads_to_wait = list(self._running_jobs.values())

        # Persist final job states OUTSIDE lock to avoid blocking
        if self._sqlite_backend:
            for jid, snapshot in jobs_snapshot.items():
                try:
                    self._persist_job_to_sqlite(jid, snapshot)
                except Exception as e:
                    logging.error(f"Failed to persist job {jid} during shutdown: {e}")

        # Wait for all threads to complete (outside of lock to avoid deadlock)
        for thread in threads_to_wait:
            if thread.is_alive():
                try:
                    thread.join(timeout=5.0)
                    if thread.is_alive():
                        logging.warning(
                            f"Thread {thread.name} did not complete gracefully within 5 seconds"
                        )
                except Exception as e:
                    logging.error(f"Error waiting for thread to complete: {e}")

        logging.info("Background job manager shutdown complete")

    def get_jobs_by_operation_and_params(
        self, operation_types: list[str], params_filter: Optional[Dict[str, Any]] = None
    ) -> list[Dict[str, Any]]:
        """
        Get jobs by operation type and optional parameter filtering.

        This is a simplified implementation for repository deletion job cancellation.
        In a real implementation, this would parse job parameters and filter accordingly.

        Args:
            operation_types: List of operation types to filter by
            params_filter: Optional dictionary of parameters to match (currently unused)

        Returns:
            List of job dictionaries matching the criteria
        """
        with self._lock:
            matching_jobs = []
            for job in self.jobs.values():
                if job.operation_type in operation_types:
                    # For now, return basic job info
                    # In a real implementation, we'd parse stored parameters and filter
                    job_dict = {
                        "job_id": job.job_id,
                        "operation_type": job.operation_type,
                        "status": job.status.value,
                        "username": job.username,
                        "created_at": job.created_at.isoformat(),
                        "started_at": (
                            job.started_at.isoformat() if job.started_at else None
                        ),
                        "completed_at": (
                            job.completed_at.isoformat() if job.completed_at else None
                        ),
                        "progress": job.progress,
                        "result": job.result,
                        "error": job.error,
                    }
                    matching_jobs.append(job_dict)

            return matching_jobs

    def _snapshot_job(self, job: "BackgroundJob") -> Dict[str, Any]:
        """Create a serializable snapshot of job state for persistence outside lock.

        Story #267 Component 4: This snapshot is taken under the lock, then the
        actual SQLite I/O happens outside the lock using this snapshot.
        """
        return {
            "job_id": job.job_id,
            "operation_type": job.operation_type,
            "status": job.status.value if hasattr(job.status, "value") else job.status,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "result": job.result,
            "error": job.error,
            "progress": job.progress,
            "username": job.username,
            "is_admin": job.is_admin,
            "cancelled": job.cancelled,
            "repo_alias": job.repo_alias,
            "resolution_attempts": job.resolution_attempts,
            "claude_actions": job.claude_actions,
            "failure_reason": job.failure_reason,
            "extended_error": job.extended_error,
            "language_resolution_status": job.language_resolution_status,
        }

    def _persist_job_to_sqlite(self, job_id: str, snapshot: Dict[str, Any]) -> None:
        """Persist a single job snapshot to SQLite without holding the memory lock.

        Story #267 Component 4: This method does SQLite I/O outside the lock.
        If the write fails, the in-memory state is still correct.
        """
        if not self._sqlite_backend:
            return
        try:
            existing = self._sqlite_backend.get_job(job_id)
            if existing:
                self._sqlite_backend.update_job(
                    job_id=job_id,
                    status=snapshot["status"],
                    started_at=snapshot["started_at"],
                    completed_at=snapshot["completed_at"],
                    result=snapshot["result"],
                    error=snapshot["error"],
                    progress=snapshot["progress"],
                    cancelled=snapshot["cancelled"],
                    resolution_attempts=snapshot["resolution_attempts"],
                    claude_actions=snapshot["claude_actions"],
                    failure_reason=snapshot["failure_reason"],
                    extended_error=snapshot["extended_error"],
                    language_resolution_status=snapshot["language_resolution_status"],
                )
            else:
                self._sqlite_backend.save_job(
                    job_id=snapshot["job_id"],
                    operation_type=snapshot["operation_type"],
                    status=snapshot["status"],
                    created_at=snapshot["created_at"],
                    started_at=snapshot["started_at"],
                    completed_at=snapshot["completed_at"],
                    result=snapshot["result"],
                    error=snapshot["error"],
                    progress=snapshot["progress"],
                    username=snapshot["username"],
                    is_admin=snapshot["is_admin"],
                    cancelled=snapshot["cancelled"],
                    repo_alias=snapshot["repo_alias"],
                    resolution_attempts=snapshot["resolution_attempts"],
                    claude_actions=snapshot["claude_actions"],
                    failure_reason=snapshot["failure_reason"],
                    extended_error=snapshot["extended_error"],
                    language_resolution_status=snapshot["language_resolution_status"],
                )
        except Exception as e:
            logging.error(f"Failed to persist job {job_id} to SQLite: {e}")

    def _persist_jobs(self, job_id: Optional[str] = None) -> None:
        """
        Persist jobs to storage (SQLite or JSON file).

        Story #267 Components 1-2: When job_id is provided, only that job is persisted
        (single-job path). When job_id is None, all jobs are persisted (shutdown/bulk).

        Note: For SQLite single-job persist, this method can be called outside the lock
        since it uses _persist_job_to_sqlite which does its own error handling.
        For full persist and JSON, this should be called within a lock.
        """
        # Use SQLite backend if enabled
        if self._sqlite_backend:
            if job_id is not None:
                # Story #267 Component 1: Single-job persist (2 ops instead of 20K)
                self._persist_single_job_sqlite(job_id)
            else:
                # Full persist (shutdown only)
                self._persist_jobs_sqlite()
            return

        # Fall back to JSON file storage
        if not self.storage_path:
            return

        try:
            storage_file = Path(self.storage_path)
            storage_file.parent.mkdir(parents=True, exist_ok=True)

            # Convert jobs to serializable format
            serializable_jobs = {}
            for jid, job in self.jobs.items():
                job_dict = asdict(job)
                # Convert datetime objects to ISO strings
                for field in ["created_at", "started_at", "completed_at"]:
                    if job_dict[field] is not None:
                        job_dict[field] = job_dict[field].isoformat()
                # Convert enum to string
                job_dict["status"] = (
                    job_dict["status"].value
                    if hasattr(job_dict["status"], "value")
                    else job_dict["status"]
                )
                serializable_jobs[jid] = job_dict

            with open(storage_file, "w") as f:
                json.dump(serializable_jobs, f, indent=2)

        except Exception as e:
            # Log the error but don't raise - persistence failures shouldn't break the system
            # Jobs should still work in memory even if persistence fails
            logging.error(f"Failed to persist jobs: {e}")

    def _persist_single_job_sqlite(self, job_id: str) -> None:
        """Persist a single job to SQLite.

        Story #267 Component 1: Instead of iterating all jobs (20K ops),
        persist only the one job that changed (2 ops: get + save/update).
        """
        if not self._sqlite_backend:
            return

        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return  # Job was removed between lock release and persist
            snapshot = self._snapshot_job(job)

        self._persist_job_to_sqlite(job_id, snapshot)

    def _persist_jobs_sqlite(self) -> None:
        """
        Persist all in-memory jobs to SQLite.

        Note: This method should be called within a lock.
        """
        if not self._sqlite_backend:
            return

        try:
            for job_id, job in self.jobs.items():
                # Check if job exists in database
                existing = self._sqlite_backend.get_job(job_id)
                if existing:
                    # Update existing job
                    self._sqlite_backend.update_job(
                        job_id=job_id,
                        status=job.status.value,
                        started_at=(
                            job.started_at.isoformat() if job.started_at else None
                        ),
                        completed_at=(
                            job.completed_at.isoformat() if job.completed_at else None
                        ),
                        result=job.result,
                        error=job.error,
                        progress=job.progress,
                        cancelled=job.cancelled,
                        resolution_attempts=job.resolution_attempts,
                        claude_actions=job.claude_actions,
                        failure_reason=job.failure_reason,
                        extended_error=job.extended_error,
                        language_resolution_status=job.language_resolution_status,
                    )
                else:
                    # Insert new job
                    self._sqlite_backend.save_job(
                        job_id=job_id,
                        operation_type=job.operation_type,
                        status=job.status.value,
                        created_at=job.created_at.isoformat(),
                        started_at=(
                            job.started_at.isoformat() if job.started_at else None
                        ),
                        completed_at=(
                            job.completed_at.isoformat() if job.completed_at else None
                        ),
                        result=job.result,
                        error=job.error,
                        progress=job.progress,
                        username=job.username,
                        is_admin=job.is_admin,
                        cancelled=job.cancelled,
                        repo_alias=job.repo_alias,
                        resolution_attempts=job.resolution_attempts,
                        claude_actions=job.claude_actions,
                        failure_reason=job.failure_reason,
                        extended_error=job.extended_error,
                        language_resolution_status=job.language_resolution_status,
                    )
        except Exception as e:
            logging.error(f"Failed to persist jobs to SQLite: {e}")

    # Maximum number of jobs to load from SQLite into memory at startup (legacy)
    MAX_JOBS_TO_LOAD = 10000

    # Story #267 Component 8: Safety cap for active jobs loaded per status at startup.
    # Normally < 20 active jobs, but cap at 500 to prevent unbounded memory usage.
    MAX_ACTIVE_JOBS_PER_STATUS = 500

    def _load_jobs(self) -> None:
        """
        Load jobs from storage (SQLite or JSON file).
        """
        # Use SQLite backend if enabled
        if self._sqlite_backend:
            self._load_jobs_sqlite()
            return

        # Fall back to JSON file storage
        if not self.storage_path:
            return

        try:
            storage_file = Path(self.storage_path)
            if not storage_file.exists():
                return

            with open(storage_file, "r") as f:
                stored_jobs = json.load(f)

            for job_id, job_dict in stored_jobs.items():
                # Convert ISO strings back to datetime objects
                for field in ["created_at", "started_at", "completed_at"]:
                    if job_dict[field] is not None:
                        job_dict[field] = datetime.fromisoformat(job_dict[field])

                # Convert string status back to enum
                job_dict["status"] = JobStatus(job_dict["status"])

                # Create job object
                job = BackgroundJob(**job_dict)
                self.jobs[job_id] = job

            logging.info(f"Loaded {len(stored_jobs)} jobs from storage")

        except Exception as e:
            logging.error(f"Failed to load jobs from storage: {e}")

    def _load_jobs_sqlite(self) -> None:
        """
        Load jobs from SQLite database into memory.

        Story #723: Clean up orphaned jobs before loading.
        On server restart, any 'running' or 'pending' jobs are orphaned
        since the processes executing them no longer exist.

        Story #267 Component 6: Clean up old completed/failed jobs before loading.
        Story #267 Component 8: Load only active/pending jobs (not all 10K).
        """
        if not self._sqlite_backend:
            return

        try:
            # Story #723: Clean up orphaned jobs on server startup
            # This must happen BEFORE loading jobs into memory to ensure
            # the in-memory state reflects the cleaned-up database state
            orphan_count = self._sqlite_backend.cleanup_orphaned_jobs_on_startup()
            if orphan_count > 0:
                logging.info(
                    f"Cleaned up {orphan_count} orphaned jobs on server startup"
                )

            # Story #267 Component 6: Startup cleanup of old completed/failed jobs
            max_age = self._background_jobs_config.cleanup_max_age_hours
            old_cleaned = self._sqlite_backend.cleanup_old_jobs(max_age)
            if old_cleaned > 0:
                logging.info(
                    f"Startup cleanup: removed {old_cleaned} old jobs from SQLite "
                    f"(older than {max_age} hours)"
                )

            # Story #267 Component 8: Load only active/pending jobs into memory.
            # After orphan cleanup marks running/pending as failed, there should
            # be very few active jobs. Historical data is served from SQLite directly.
            loaded_count = 0
            for status_value in ["running", "pending"]:
                status_jobs = self._sqlite_backend.list_jobs(
                    status=status_value,
                    limit=self.MAX_ACTIVE_JOBS_PER_STATUS,
                )
                for job_dict in status_jobs:
                    self._deserialize_and_add_job(job_dict)
                    loaded_count += 1

            logging.info(f"Loaded {loaded_count} active jobs from SQLite into memory")

        except Exception as e:
            logging.error(f"Failed to load jobs from SQLite: {e}")

    def _deserialize_and_add_job(self, job_dict: Dict[str, Any]) -> None:
        """Deserialize a job dictionary from SQLite and add to in-memory dict.

        Story #267 Component 8: Extracted helper to avoid duplicating deserialization
        logic when loading jobs per-status in _load_jobs_sqlite.
        """
        # Convert ISO strings back to datetime objects
        for field_name in ["created_at", "started_at", "completed_at"]:
            if job_dict.get(field_name) is not None:
                job_dict[field_name] = datetime.fromisoformat(job_dict[field_name])

        # Convert string status back to enum
        job_dict["status"] = JobStatus(job_dict["status"])

        # Create job object
        job = BackgroundJob(**job_dict)
        self.jobs[job_dict["job_id"]] = job

    def _calculate_cutoff(self, time_filter: str) -> datetime:
        """
        Calculate cutoff datetime based on time filter.

        Args:
            time_filter: Time filter string ("24h", "7d", "30d")

        Returns:
            Cutoff datetime (timezone-aware UTC)
        """
        now = datetime.now(timezone.utc)

        if time_filter == "24h":
            return now - timedelta(hours=24)
        elif time_filter == "7d":
            return now - timedelta(days=7)
        elif time_filter == "30d":
            return now - timedelta(days=30)
        else:
            # Default to 24h for invalid filters
            return now - timedelta(hours=24)

    def get_job_stats_with_filter(self, time_filter: str = "24h") -> Dict[str, int]:
        """
        Get job statistics filtered by time period.

        Story #267 Component 8: Query SQLite directly for historical stats since
        completed/failed jobs are no longer kept in memory.

        Args:
            time_filter: Time filter string ("24h", "7d", "30d")

        Returns:
            Dictionary with "completed" and "failed" counts
        """
        # Story #267 Component 8: Use SQLite for stats (completed/failed not in memory)
        if self._sqlite_backend:
            try:
                return self._sqlite_backend.get_job_stats(time_filter)
            except Exception as e:
                logging.error(f"Failed to get job stats from SQLite: {e}")
                return {"completed": 0, "failed": 0}

        # Fallback: iterate in-memory jobs (for JSON storage or no backend)
        cutoff_time = self._calculate_cutoff(time_filter)

        with self._lock:
            completed = 0
            failed = 0

            for job in self.jobs.values():
                # Only count jobs with completion time after cutoff
                if job.completed_at and job.completed_at >= cutoff_time:
                    if job.status == JobStatus.COMPLETED:
                        completed += 1
                    elif job.status == JobStatus.FAILED:
                        failed += 1

            return {"completed": completed, "failed": failed}

    def get_recent_jobs_with_filter(
        self, time_filter: str = "30d", limit: int = 20
    ) -> list[Dict[str, Any]]:
        """
        Get recent jobs filtered by time period.

        Story #4 AC1: Includes RUNNING, PENDING, COMPLETED, and FAILED jobs.
        Running/pending jobs appear at the top of the list before completed jobs.

        Story #267 Component 8: Active jobs come from memory, historical from SQLite.

        Args:
            time_filter: Time filter string ("24h", "7d", "30d"), default "30d"
            limit: Maximum number of jobs to return, default 20

        Returns:
            List of job dictionaries with running/pending first, then sorted by time
        """
        cutoff_time = self._calculate_cutoff(time_filter)
        recent_jobs = []
        seen_job_ids: set = set()

        # Step 1: Get active jobs from memory (running/pending)
        with self._lock:
            for job in self.jobs.values():
                include_job = False

                if job.status in [JobStatus.RUNNING, JobStatus.PENDING]:
                    if job.created_at >= cutoff_time:
                        include_job = True
                elif job.status in [JobStatus.COMPLETED, JobStatus.FAILED]:
                    if job.completed_at and job.completed_at >= cutoff_time:
                        include_job = True

                if include_job:
                    job_dict = {
                        "job_id": job.job_id,
                        "operation_type": job.operation_type,
                        "status": job.status.value,
                        "created_at": job.created_at.isoformat(),
                        "started_at": (
                            job.started_at.isoformat() if job.started_at else None
                        ),
                        "completed_at": (
                            job.completed_at.isoformat() if job.completed_at else None
                        ),
                        "progress": job.progress,
                        "result": job.result,
                        "error": job.error,
                        "username": job.username,
                        "repo_alias": job.repo_alias,
                    }
                    recent_jobs.append(job_dict)
                    seen_job_ids.add(job.job_id)

        # Step 2: Story #267 Component 8 - Get historical jobs from SQLite
        if self._sqlite_backend:
            try:
                db_jobs = self._sqlite_backend.list_jobs(limit=limit)
                for db_job in db_jobs:
                    if db_job["job_id"] not in seen_job_ids:
                        # Apply time filter
                        include_db_job = False
                        status = db_job.get("status", "")
                        if status in ("running", "pending"):
                            created_str = db_job.get("created_at")
                            if created_str:
                                created_dt = datetime.fromisoformat(created_str)
                                if created_dt >= cutoff_time:
                                    include_db_job = True
                        elif status in ("completed", "failed"):
                            completed_str = db_job.get("completed_at")
                            if completed_str:
                                completed_dt = datetime.fromisoformat(completed_str)
                                if completed_dt >= cutoff_time:
                                    include_db_job = True

                        if include_db_job:
                            recent_jobs.append(db_job)
                            seen_job_ids.add(db_job["job_id"])
            except Exception as e:
                logging.error(f"Failed to get recent jobs from SQLite: {e}")

        # Story #4 AC1: Sort with running/pending jobs first, then by time
        # Priority: RUNNING (0) > PENDING (1) > COMPLETED/FAILED (2)
        # Within each priority, sort by relevant time (newest first)
        def sort_key(x):
            status = x["status"]
            if status == "running":
                priority = 0
                # Use started_at for running jobs, fallback to created_at
                time_str = x.get("started_at") or x.get("created_at")
            elif status == "pending":
                priority = 1
                # Use created_at for pending jobs
                time_str = x.get("created_at")
            else:
                priority = 2
                # Use completed_at for completed/failed jobs
                time_str = x.get("completed_at") or x.get("created_at")

            # Parse time for sorting (newest first = reverse, so negate)
            if time_str:
                dt = datetime.fromisoformat(time_str)
            else:
                dt = datetime.min.replace(tzinfo=timezone.utc)

            return (priority, -dt.timestamp())

        recent_jobs.sort(key=sort_key)

        # Return up to limit jobs
        return recent_jobs[:limit]

    # ------------------------------------------------------------------
    # Story #271: Display-layer job retrieval with filtering and pagination
    # ------------------------------------------------------------------

    def _normalize_job_to_display_dict(self, job) -> Dict[str, Any]:
        """Convert a BackgroundJob object or SQLite row dict to a display dict.

        Produces a consistent dict shape regardless of source (memory vs DB),
        matching exactly what the jobs.html template expects.

        Args:
            job: Either a BackgroundJob dataclass instance or a dict from
                 BackgroundJobsSqliteBackend._row_to_dict()

        Returns:
            Dict with all required display keys.
        """
        if isinstance(job, BackgroundJob):
            # --- In-memory BackgroundJob object ---
            status_str = (
                job.status.value if hasattr(job.status, "value") else str(job.status)
            )
            created_at = job.created_at.isoformat() if job.created_at else None
            started_at = job.started_at.isoformat() if job.started_at else None
            completed_at = job.completed_at.isoformat() if job.completed_at else None

            # Determine repository_name: prefer repo_alias, fall back to result dict
            repository_name = getattr(job, "repo_alias", None)
            if not repository_name and job.result and isinstance(job.result, dict):
                repository_name = job.result.get("alias") or job.result.get("repository")

            # Calculate duration_seconds for completed jobs
            duration_seconds = None
            if job.completed_at and job.started_at:
                try:
                    completed = job.completed_at
                    started = job.started_at
                    completed_aware = hasattr(completed, "tzinfo") and completed.tzinfo is not None
                    started_aware = hasattr(started, "tzinfo") and started.tzinfo is not None
                    if completed_aware and not started_aware:
                        completed = completed.replace(tzinfo=None)
                    elif started_aware and not completed_aware:
                        started = started.replace(tzinfo=None)
                    duration_seconds = int((completed - started).total_seconds())
                except (TypeError, AttributeError):
                    duration_seconds = None

            return {
                "job_id": job.job_id,
                "job_type": job.operation_type,
                "operation_type": job.operation_type,
                "status": status_str,
                "progress": job.progress,
                "created_at": created_at,
                "started_at": started_at,
                "completed_at": completed_at,
                "error_message": job.error,
                "username": job.username,
                "user_alias": getattr(job, "user_alias", None),
                "repository_name": repository_name,
                "repository_url": getattr(job, "repository_url", None),
                "progress_info": getattr(job, "progress_info", None),
                "duration_seconds": duration_seconds,
            }
        else:
            # --- SQLite row dict (from _row_to_dict) ---
            started_at_str = job.get("started_at")
            completed_at_str = job.get("completed_at")

            # Calculate duration_seconds from ISO strings
            duration_seconds = None
            if started_at_str and completed_at_str:
                try:
                    started_dt = datetime.fromisoformat(started_at_str)
                    completed_dt = datetime.fromisoformat(completed_at_str)
                    duration_seconds = int((completed_dt - started_dt).total_seconds())
                except (ValueError, TypeError):
                    duration_seconds = None

            operation_type = job.get("operation_type") or ""
            return {
                "job_id": job.get("job_id"),
                "job_type": operation_type,
                "operation_type": operation_type,
                "status": job.get("status"),
                "progress": job.get("progress", 0),
                "created_at": job.get("created_at"),
                "started_at": started_at_str,
                "completed_at": completed_at_str,
                "error_message": job.get("error"),
                "username": job.get("username"),
                "user_alias": job.get("username"),  # DB jobs have no separate user_alias
                "repository_name": job.get("repo_alias"),
                "repository_url": None,  # Not stored in DB
                "progress_info": None,   # Not stored in DB
                "duration_seconds": duration_seconds,
            }

    def get_jobs_for_display(
        self,
        status_filter: str = None,
        type_filter: str = None,
        search_text: str = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple:
        """Return (jobs_list, total_count, total_pages) merging memory + SQLite.

        Story #271 Component 2: Filtered, paginated jobs for the Web UI jobs page.

        Algorithm:
        1. Collect active jobs (running/pending) from self.jobs under self._lock.
        2. Apply filters to active jobs in Python (small set).
        3. Build seen_ids set from active jobs to avoid duplicates from DB.
        4. Query SQLite via list_jobs_filtered() with filters + exclude_ids=seen_ids.
        5. Merge active + DB jobs; compute total_count and total_pages.
        6. Apply pagination (page, page_size).

        Args:
            status_filter: Exact status string to filter by (e.g. 'completed').
            type_filter: Exact operation_type string to filter by.
            search_text: Case-insensitive substring to search repo name / username.
            page: 1-based page number.
            page_size: Number of results per page.

        Returns:
            Tuple of (jobs: List[Dict], total_count: int, total_pages: int).
        """
        # Step 1: Collect active (running/pending) memory jobs
        active_jobs: List[Dict[str, Any]] = []
        seen_ids: set = set()

        with self._lock:
            for job in self.jobs.values():
                if job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
                    continue

                # Apply filters to memory jobs in Python
                status_str = job.status.value if hasattr(job.status, "value") else str(job.status)
                if status_filter and status_str != status_filter:
                    continue
                if type_filter and job.operation_type != type_filter:
                    continue
                if search_text:
                    search_lower = search_text.lower()
                    repo = (getattr(job, "repo_alias", None) or "").lower()
                    user = (job.username or "").lower()
                    op = (job.operation_type or "").lower()
                    err = (job.error or "").lower()
                    if not (
                        search_lower in repo
                        or search_lower in user
                        or search_lower in op
                        or search_lower in err
                    ):
                        continue

                active_jobs.append(self._normalize_job_to_display_dict(job))
                seen_ids.add(job.job_id)

        # Step 2: Query SQLite for historical / non-active jobs with same filters
        db_jobs_normalized: List[Dict[str, Any]] = []
        db_total_from_sqlite = 0

        if self._sqlite_backend:
            try:
                db_rows, db_count = self._sqlite_backend.list_jobs_filtered(
                    status=status_filter,
                    operation_type=type_filter,
                    search_text=search_text,
                    exclude_ids=seen_ids if seen_ids else None,
                )
                db_total_from_sqlite = db_count
                for row in db_rows:
                    db_jobs_normalized.append(self._normalize_job_to_display_dict(row))
            except Exception as e:
                logging.error(f"Failed to get jobs for display from SQLite: {e}")

        # Step 3: Merge and compute total_count
        all_jobs = active_jobs + db_jobs_normalized
        total_count = len(active_jobs) + db_total_from_sqlite

        # Step 4: Paginate
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        offset = (page - 1) * page_size
        paginated = all_jobs[offset: offset + page_size]

        return paginated, total_count, total_pages
