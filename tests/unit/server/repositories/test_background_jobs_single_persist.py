"""
Unit tests for BackgroundJobManager single-job persistence (Story #267, Components 1-3).

Tests that _persist_jobs(job_id) persists only the specified job instead of all jobs,
and that all call sites pass the correct job_id.
"""

import tempfile
import time
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig


class TestSingleJobPersist:
    """Test single-job persistence (Components 1-3)."""

    def setup_method(self):
        """Setup test environment with SQLite backend."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        # Initialize database schema (creates background_jobs table)
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
        )

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_persist_jobs_with_job_id_only_persists_that_job(self):
        """Component 1-2: _persist_jobs(job_id) should only persist the specified job.

        Create manager with 100 jobs. Call _persist_jobs(job_id="specific-id").
        Assert only 1 save_job or update_job call on the SQLite backend (not 100).
        """
        # Arrange: Add 100 jobs directly to memory
        for i in range(100):
            jid = f"job-{i}"
            self.manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="test_op",
                status=JobStatus.COMPLETED,
                created_at=datetime.now(timezone.utc),
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                result={"status": "success"},
                error=None,
                progress=100,
                username="testuser",
            )

        # Persist all to DB first so they exist
        self.manager._persist_jobs()

        # Now track calls to the backend
        original_get_job = self.manager._sqlite_backend.get_job
        original_update_job = self.manager._sqlite_backend.update_job

        get_job_calls = []
        update_job_calls = []
        save_job_calls = []

        def tracking_get_job(job_id):
            get_job_calls.append(job_id)
            return original_get_job(job_id)

        def tracking_update_job(job_id, **kwargs):
            update_job_calls.append(job_id)
            return original_update_job(job_id, **kwargs)

        def tracking_save_job(**kwargs):
            save_job_calls.append(kwargs.get("job_id"))

        self.manager._sqlite_backend.get_job = tracking_get_job
        self.manager._sqlite_backend.update_job = tracking_update_job
        self.manager._sqlite_backend.save_job = tracking_save_job

        # Act: Persist only job-50
        self.manager._persist_jobs(job_id="job-50")

        # Assert: Only 1 get_job call (for job-50) and 1 update_job call
        assert len(get_job_calls) == 1, f"Expected 1 get_job call, got {len(get_job_calls)}"
        assert get_job_calls[0] == "job-50"
        assert len(update_job_calls) == 1, f"Expected 1 update_job call, got {len(update_job_calls)}"
        assert update_job_calls[0] == "job-50"
        assert len(save_job_calls) == 0, "Should not call save_job for existing jobs"

    def test_persist_jobs_without_job_id_persists_all(self):
        """Component 2: _persist_jobs() without job_id should persist all jobs (backward compat)."""
        # Arrange: Add 5 jobs
        for i in range(5):
            jid = f"job-{i}"
            self.manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="test_op",
                status=JobStatus.COMPLETED,
                created_at=datetime.now(timezone.utc),
                started_at=None,
                completed_at=datetime.now(timezone.utc),
                result=None,
                error=None,
                progress=100,
                username="testuser",
            )

        # Act: Persist all (no job_id)
        self.manager._persist_jobs()

        # Assert: All 5 jobs should be in the database
        for i in range(5):
            job_data = self.manager._sqlite_backend.get_job(f"job-{i}")
            assert job_data is not None, f"Job job-{i} should be in database"

    def test_submit_job_persists_only_new_job(self):
        """Component 3: submit_job should persist only the newly submitted job."""
        # Arrange: Pre-populate with existing jobs
        for i in range(10):
            jid = f"existing-{i}"
            self.manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="old_op",
                status=JobStatus.COMPLETED,
                created_at=datetime.now(timezone.utc),
                started_at=None,
                completed_at=datetime.now(timezone.utc),
                result=None,
                error=None,
                progress=100,
                username="testuser",
            )
        # Save existing jobs to DB
        self.manager._persist_jobs()

        # Track backend calls from this point
        original_update_job = self.manager._sqlite_backend.update_job
        original_save_job = self.manager._sqlite_backend.save_job
        update_calls = []
        save_calls = []

        def tracking_update(job_id, **kwargs):
            update_calls.append(job_id)
            return original_update_job(job_id, **kwargs)

        def tracking_save(**kwargs):
            save_calls.append(kwargs.get("job_id"))
            return original_save_job(**kwargs)

        self.manager._sqlite_backend.update_job = tracking_update
        self.manager._sqlite_backend.save_job = tracking_save

        # Act: Submit a new job
        def dummy_task():
            return {"status": "success"}

        job_id = self.manager.submit_job(
            "new_operation", dummy_task, submitter_username="testuser"
        )

        # Wait for job to complete
        time.sleep(0.3)

        # Assert: The new job was saved (save_job call for the new job)
        assert job_id in save_calls, f"New job {job_id} should have been saved. save_calls={save_calls}"
        # No updates for the 10 existing jobs during submit
        existing_updates = [jid for jid in update_calls if jid.startswith("existing-")]
        assert len(existing_updates) == 0, (
            f"Existing jobs should not be updated during submit. "
            f"Got updates for: {existing_updates}"
        )

    def test_progress_callback_persists_single_job(self):
        """Component 3: Progress callback should persist only the updated job."""
        # Arrange: Pre-populate with existing jobs
        for i in range(5):
            jid = f"bg-{i}"
            self.manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="old_op",
                status=JobStatus.COMPLETED,
                created_at=datetime.now(timezone.utc),
                started_at=None,
                completed_at=datetime.now(timezone.utc),
                result=None,
                error=None,
                progress=100,
                username="testuser",
            )
        self.manager._persist_jobs()

        # Track calls
        persist_calls = []
        original_persist = self.manager._persist_jobs

        def tracking_persist(job_id=None):
            persist_calls.append(job_id)
            return original_persist(job_id=job_id)

        self.manager._persist_jobs = tracking_persist

        # Act: Submit a job with progress callback
        def task_with_progress(progress_callback=None):
            if progress_callback:
                progress_callback(50)
                progress_callback(75)
            return {"status": "success"}

        job_id = self.manager.submit_job(
            "progress_op", task_with_progress, submitter_username="testuser"
        )

        # Wait for completion
        time.sleep(0.5)

        # Assert: All persist calls should include a job_id (not None, except possibly shutdown)
        job_specific_calls = [c for c in persist_calls if c is not None]

        # Every persist during job execution should be for the specific job
        for call_job_id in job_specific_calls:
            assert call_job_id == job_id, (
                f"Persist call should be for job {job_id}, got {call_job_id}"
            )
        # At least some persist calls should have been job-specific
        assert len(job_specific_calls) >= 1, "Should have at least one job-specific persist call"

    def test_cancel_job_persists_single_job(self):
        """Component 3: cancel_job should persist only the cancelled job."""
        # Track persist calls
        persist_calls = []
        original_persist = self.manager._persist_jobs

        def tracking_persist(job_id=None):
            persist_calls.append(job_id)
            return original_persist(job_id=job_id)

        self.manager._persist_jobs = tracking_persist

        # Submit a long-running job
        def long_task():
            time.sleep(5.0)
            return {"status": "success"}

        job_id = self.manager.submit_job(
            "cancel_op", long_task, submitter_username="testuser"
        )

        # Wait for it to start
        time.sleep(0.2)

        # Clear tracking to focus on cancel
        persist_calls.clear()

        # Act: Cancel the job
        result = self.manager.cancel_job(job_id, username="testuser")
        assert result["success"] is True

        # Assert: The persist call during cancel should be for this specific job
        cancel_persist_calls = [c for c in persist_calls if c is not None]
        assert len(cancel_persist_calls) >= 1, "Cancel should trigger at least one persist"
        for call_job_id in cancel_persist_calls:
            assert call_job_id == job_id, (
                f"Cancel persist should be for job {job_id}, got {call_job_id}"
            )
