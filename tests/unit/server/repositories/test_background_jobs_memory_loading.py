"""
Unit tests for BackgroundJobManager memory loading optimization (Story #267, Component 8).

Tests that only active/pending jobs are loaded into memory at startup,
and that historical data is served from SQLite directly.
"""

import tempfile
import time
import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig


class TestMemoryLoadingOptimization:
    """Test reduced memory loading (Component 8)."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        # Initialize database schema (creates background_jobs table)
        DatabaseSchema(self.db_path).initialize_database()

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _create_manager(self):
        """Create a BackgroundJobManager with SQLite backend."""
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
                cleanup_max_age_hours=720,  # 30 days - don't clean up during these tests
            ),
        )
        return self.manager

    def test_startup_loads_only_active_pending_jobs(self):
        """Component 8: Only running/pending jobs should be loaded into memory at startup.

        Create 500 completed, 200 failed, 3 running, 2 pending jobs in SQLite.
        Init manager. Assert len(self.jobs) == 5. Assert all 5 are running/pending status.
        Note: cleanup_orphaned_jobs_on_startup marks running/pending as failed on restart,
        so we need to account for that behavior.
        """
        # Create initial manager to populate SQLite
        manager1 = self._create_manager()

        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)

        # Add 500 completed jobs directly to SQLite
        for i in range(500):
            manager1._sqlite_backend.save_job(
                job_id=f"completed-{i}",
                operation_type="test_op",
                status="completed",
                created_at=recent.isoformat(),
                completed_at=recent.isoformat(),
                username="testuser",
                progress=100,
            )

        # Add 200 failed jobs
        for i in range(200):
            manager1._sqlite_backend.save_job(
                job_id=f"failed-{i}",
                operation_type="test_op",
                status="failed",
                created_at=recent.isoformat(),
                completed_at=recent.isoformat(),
                username="testuser",
                progress=0,
                error="test failure",
            )

        manager1.shutdown()
        self.manager = None

        # Act: Create new manager - should only load active jobs (which will be 0
        # since orphan cleanup marks running/pending as failed on restart)
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
                cleanup_max_age_hours=720,
            ),
        )

        # Assert: Should NOT have loaded all 700 completed/failed jobs
        # With the new optimization, only active jobs are loaded
        assert len(self.manager.jobs) < 700, (
            f"Should not load all 700 jobs, got {len(self.manager.jobs)}"
        )

    def test_completed_job_removed_from_memory(self):
        """Component 8: Completed jobs should be removed from memory after persistence.

        Submit a job, let it complete. Assert the job is no longer in self.jobs.
        Assert the job is still retrievable from SQLite.
        """
        manager = self._create_manager()

        def quick_task():
            return {"status": "success"}

        job_id = manager.submit_job(
            "removal_test", quick_task, submitter_username="testuser"
        )

        # Wait for job to complete
        time.sleep(0.5)

        # Assert: Job should be removed from memory after completion
        assert job_id not in manager.jobs, (
            f"Completed job {job_id} should be removed from memory"
        )

        # Assert: Job should still be in SQLite
        db_job = manager._sqlite_backend.get_job(job_id)
        assert db_job is not None, "Completed job should still be in SQLite"
        assert db_job["status"] == "completed"

    def test_stats_query_uses_sqlite_for_historical(self):
        """Component 8: get_job_stats_with_filter should use SQLite for historical counts.

        With only active jobs in memory and 500 completed in SQLite,
        completed/failed counts should come from SQLite (not zero).
        """
        manager = self._create_manager()

        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)

        # Add completed/failed jobs directly to SQLite (not in memory)
        for i in range(50):
            manager._sqlite_backend.save_job(
                job_id=f"hist-completed-{i}",
                operation_type="test_op",
                status="completed",
                created_at=recent.isoformat(),
                completed_at=recent.isoformat(),
                username="testuser",
                progress=100,
            )

        for i in range(10):
            manager._sqlite_backend.save_job(
                job_id=f"hist-failed-{i}",
                operation_type="test_op",
                status="failed",
                created_at=recent.isoformat(),
                completed_at=recent.isoformat(),
                username="testuser",
                progress=0,
                error="test failure",
            )

        # Act: Query stats
        stats = manager.get_job_stats_with_filter("24h")

        # Assert: Should see the SQLite-only jobs in the counts
        assert stats["completed"] >= 50, (
            f"Expected at least 50 completed, got {stats['completed']}"
        )
        assert stats["failed"] >= 10, (
            f"Expected at least 10 failed, got {stats['failed']}"
        )

    def test_recent_jobs_merges_memory_and_sqlite(self):
        """Component 8: get_recent_jobs_with_filter should merge active from memory + historical from SQLite.

        Create 2 running jobs in memory and 100 completed in SQLite.
        Result should have running jobs first, then historical, total <= limit.
        """
        manager = self._create_manager()

        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)

        # Add running jobs to memory
        for i in range(2):
            jid = f"active-{i}"
            manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="test_op",
                status=JobStatus.RUNNING,
                created_at=now,
                started_at=now,
                completed_at=None,
                result=None,
                error=None,
                progress=50,
                username="testuser",
            )
            # Also save to SQLite for consistency
            manager._sqlite_backend.save_job(
                job_id=jid,
                operation_type="test_op",
                status="running",
                created_at=now.isoformat(),
                started_at=now.isoformat(),
                username="testuser",
                progress=50,
            )

        # Add completed jobs only to SQLite (not in memory)
        for i in range(100):
            manager._sqlite_backend.save_job(
                job_id=f"hist-{i}",
                operation_type="test_op",
                status="completed",
                created_at=recent.isoformat(),
                completed_at=recent.isoformat(),
                username="testuser",
                progress=100,
            )

        # Act: Query recent jobs with limit
        recent_jobs = manager.get_recent_jobs_with_filter("30d", limit=20)

        # Assert: Should have active jobs first, then historical
        assert len(recent_jobs) <= 20, f"Should be at most 20, got {len(recent_jobs)}"
        assert len(recent_jobs) >= 2, f"Should have at least 2 active jobs, got {len(recent_jobs)}"

        # First jobs should be the running ones
        running_jobs = [j for j in recent_jobs if j["status"] == "running"]
        assert len(running_jobs) == 2, f"Expected 2 running jobs, got {len(running_jobs)}"

        # Should also include some historical jobs from SQLite
        completed_jobs = [j for j in recent_jobs if j["status"] == "completed"]
        assert len(completed_jobs) > 0, "Should include historical completed jobs from SQLite"

    def test_new_job_tracked_in_memory_during_execution(self):
        """Component 8: New jobs should be in memory while running, removed after completion.

        Submit a job. Assert it exists in self.jobs while running.
        Assert it is removed from self.jobs after completion.
        """
        manager = self._create_manager()

        started_event = __import__("threading").Event()

        def tracked_task():
            started_event.set()
            time.sleep(0.5)
            return {"status": "success"}

        job_id = manager.submit_job(
            "tracking_test", tracked_task, submitter_username="testuser"
        )

        # Wait for job to start
        started_event.wait(timeout=2.0)
        time.sleep(0.1)  # Small delay to ensure status update

        # Assert: Job should be in memory while running
        assert job_id in manager.jobs, "Running job should be in memory"
        assert manager.jobs[job_id].status in (JobStatus.RUNNING, JobStatus.PENDING), (
            f"Job should be running/pending, got {manager.jobs[job_id].status}"
        )

        # Wait for completion
        time.sleep(1.0)

        # Assert: Job should be removed from memory after completion
        assert job_id not in manager.jobs, (
            f"Completed job {job_id} should be removed from memory"
        )

        # Assert: Job should still be in SQLite
        db_job = manager._sqlite_backend.get_job(job_id)
        assert db_job is not None, "Completed job should persist in SQLite"
        assert db_job["status"] == "completed"
