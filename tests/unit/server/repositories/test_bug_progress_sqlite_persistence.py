"""
Tests for Bug 2: progress=0 on completed/failed jobs after SQLite persistence.

Problem: When a job completes:
1. job.progress = 100 is set in memory
2. _persist_jobs() saves to SQLite
3. self.jobs.pop(job_id) removes from memory
4. Next API poll reads from SQLite — but current_phase and phase_detail
   are NOT in _snapshot_job, so they're never persisted.
   Additionally, we verify progress=100 is correctly persisted (not 0).

Fix: Add current_phase and phase_detail to _snapshot_job and to
_persist_job_to_sqlite so they are saved to SQLite and returned by
get_job_status after the job leaves memory.

Also: The SQLite table must have current_phase and phase_detail columns.
"""
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
)
from src.code_indexer.server.services.job_tracker import JobTracker
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig


class TestProgressSqlitePersistence:
    """Verify progress=100 and phase fields are persisted and readable from SQLite."""

    def setup_method(self):
        """Set up test with SQLite backend enabled."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
        )

    def teardown_method(self):
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_completed_job_progress_is_100_after_eviction_from_memory(self):
        """
        After a job completes and is evicted from memory (jobs.pop),
        get_job_status must return progress=100 (not 0).

        This tests the SQLite fallback path in get_job_status.
        """

        def simple_func():
            return {"ok": True}

        job_id = self.manager.submit_job(
            operation_type="test_completion",
            func=simple_func,
            submitter_username="testuser",
        )

        # Wait for completion
        for _ in range(50):
            status = self.manager.get_job_status(job_id, username="testuser")
            if status and status.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)

        # Job should be completed
        status = self.manager.get_job_status(job_id, username="testuser")
        assert status is not None, "Job not found after completion"
        assert status["status"] == "completed", f"Expected completed, got: {status}"

        # Ensure job has been evicted from memory (SQLite path is exercised)
        with self.manager._lock:
            job_in_memory = job_id in self.manager.jobs

        if not job_in_memory:
            # Job was evicted — re-query must return 100 from SQLite
            status = self.manager.get_job_status(job_id, username="testuser")
            assert status is not None, "Job not found in SQLite after eviction"
            assert status.get("progress") == 100, (
                f"Bug 2: Expected progress=100 after job eviction from memory, "
                f"but got progress={status.get('progress')}. "
                f"Full status: {status}"
            )
        else:
            # Job still in memory — verify from memory it's 100
            assert status.get("progress") == 100, (
                f"Expected progress=100, got {status.get('progress')}"
            )

    def test_completed_job_progress_100_persisted_to_sqlite_directly(self):
        """
        The SQLite backend must store progress=100 for a completed job.

        This directly tests _persist_job_to_sqlite after setting progress=100.
        """
        from src.code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )
        from datetime import datetime, timezone

        job_id = "test-job-sqlite-progress"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            result={"ok": True},
            error=None,
            progress=100,
            username="testuser",
        )

        # Put in memory and persist
        self.manager.jobs[job_id] = job
        self.manager._persist_jobs(job_id=job_id)

        # Read from SQLite directly
        db_job = self.manager._sqlite_backend.get_job(job_id)
        assert db_job is not None, "Job not found in SQLite"
        assert db_job.get("progress") == 100, (
            f"Bug 2: SQLite backend returned progress={db_job.get('progress')} "
            f"instead of 100. Full row: {db_job}"
        )

    def test_current_phase_persisted_to_sqlite(self):
        """
        current_phase set on a job must be persisted to SQLite and
        readable via get_job_status after the job leaves memory.

        This tests that _snapshot_job includes current_phase.
        """
        from src.code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )
        from datetime import datetime, timezone

        job_id = "test-job-current-phase"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            result={"ok": True},
            error=None,
            progress=75,
            username="testuser",
            current_phase="semantic",
        )

        # Put in memory and persist
        self.manager.jobs[job_id] = job
        self.manager._persist_jobs(job_id=job_id)

        # Now remove from memory to force SQLite fallback
        with self.manager._lock:
            self.manager.jobs.pop(job_id, None)

        # Get status from SQLite
        status = self.manager.get_job_status(job_id, username="testuser")
        assert status is not None, "Job not found in SQLite after eviction"
        assert status.get("current_phase") == "semantic", (
            f"Bug 2: current_phase not persisted. Expected 'semantic', "
            f"got {status.get('current_phase')}. Full status: {status}"
        )

    def test_phase_detail_persisted_to_sqlite(self):
        """
        phase_detail set on a job must be persisted to SQLite and
        readable via get_job_status after the job leaves memory.

        This tests that _snapshot_job includes phase_detail.
        """
        from src.code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )
        from datetime import datetime, timezone

        job_id = "test-job-phase-detail"
        job = BackgroundJob(
            job_id=job_id,
            operation_type="test_op",
            status=JobStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            result={"ok": True},
            error=None,
            progress=50,
            username="testuser",
            phase_detail="150/500 files indexed",
        )

        # Put in memory and persist
        self.manager.jobs[job_id] = job
        self.manager._persist_jobs(job_id=job_id)

        # Remove from memory to force SQLite fallback
        with self.manager._lock:
            self.manager.jobs.pop(job_id, None)

        # Get status from SQLite
        status = self.manager.get_job_status(job_id, username="testuser")
        assert status is not None, "Job not found in SQLite after eviction"
        assert status.get("phase_detail") == "150/500 files indexed", (
            f"Bug 2: phase_detail not persisted. "
            f"Expected '150/500 files indexed', "
            f"got {status.get('phase_detail')}. Full status: {status}"
        )

    def test_progress_100_readable_via_get_job_status_sqlite_path(self):
        """
        Integration test: full job lifecycle with SQLite backend.

        A job that emits progress via progress_callback, then completes,
        must show progress=100 when queried after eviction from memory.
        """

        def func_with_progress(progress_callback=None):
            if progress_callback:
                progress_callback(0, phase="semantic", detail="starting")
                progress_callback(50, phase="semantic", detail="halfway")
                progress_callback(100, phase="semantic", detail="done")
            return {"success": True}

        job_id = self.manager.submit_job(
            operation_type="test_full_lifecycle",
            func=func_with_progress,
            submitter_username="testuser",
        )

        # Wait for completion (must be in terminal state before eviction)
        final_status = None
        for _ in range(100):
            status = self.manager.get_job_status(job_id, username="testuser")
            if status and status.get("status") in ("completed", "failed", "cancelled"):
                final_status = status
                break
            time.sleep(0.05)

        assert final_status is not None, "Job did not reach terminal state in time"
        assert final_status.get("status") == "completed", (
            f"Job failed instead of completing: {final_status}"
        )

        # Now force eviction from memory to test SQLite path
        # (in SQLite mode, completed jobs may already have been evicted)
        with self.manager._lock:
            self.manager.jobs.pop(job_id, None)

        # Query from SQLite
        status = self.manager.get_job_status(job_id, username="testuser")
        assert status is not None, "Job not found in SQLite"
        assert status.get("status") == "completed", f"Unexpected status: {status}"
        assert status.get("progress") == 100, (
            f"Bug 2: progress={status.get('progress')} after full lifecycle. "
            f"Expected 100. Full status: {status}"
        )


class TestJobTrackerCompleteJobProgress:
    """
    Verify that JobTracker.complete_job() writes progress=100 to SQLite.

    Bug: complete_job() called _upsert_job(job) where job.progress defaulted
    to 0 (TrackedJob default), overwriting the correct progress=100 that
    BackgroundJobManager had already persisted.

    Fix: complete_job() now sets job.progress = 100 before _upsert_job(job).
    """

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.tracker = JobTracker(self.db_path)

    def teardown_method(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_complete_job_writes_progress_100_to_sqlite(self):
        """
        After JobTracker.complete_job(), the SQLite record must have progress=100.

        This directly tests the fix: job.progress = 100 added in complete_job()
        before _upsert_job(job), so the UPDATE statement writes 100 not 0.
        """
        job_id = "tracker-test-complete-progress"

        # Register the job (status=pending, progress=0)
        self.tracker.register_job(
            job_id=job_id,
            operation_type="test_op",
            username="testuser",
        )

        # Transition to running and update progress to 50 to simulate work
        self.tracker.update_status(job_id, status="running", progress=50)

        # Complete the job
        self.tracker.complete_job(job_id, result={"ok": True})

        # Query SQLite directly via get_job (falls back to SQLite since job
        # was removed from _active_jobs by complete_job)
        job = self.tracker.get_job(job_id)
        assert job is not None, "Job not found in SQLite after complete_job()"
        assert job.status == "completed", (
            f"Expected status='completed', got '{job.status}'"
        )
        assert job.progress == 100, (
            f"JobTracker.complete_job() bug: progress={job.progress} "
            f"instead of 100. The _upsert_job call overwrote progress with 0."
        )

    def test_complete_job_progress_100_when_previous_progress_was_zero(self):
        """
        complete_job() must write progress=100 even when no progress updates
        were made (job.progress was never set, stays at TrackedJob default 0).
        """
        job_id = "tracker-test-complete-zero-progress"

        self.tracker.register_job(
            job_id=job_id,
            operation_type="test_op_no_progress",
            username="testuser",
        )
        self.tracker.update_status(job_id, status="running")

        # Complete without any progress updates (progress is still 0)
        self.tracker.complete_job(job_id)

        job = self.tracker.get_job(job_id)
        assert job is not None, "Job not found in SQLite after complete_job()"
        assert job.progress == 100, (
            f"complete_job() must set progress=100 regardless of prior value. "
            f"Got progress={job.progress}."
        )
