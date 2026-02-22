"""
Unit tests for BackgroundJobManager cleanup wiring (Story #267, Components 5-7).

Tests that cleanup_old_jobs() removes from both memory AND SQLite,
startup cleanup runs before loading, and cleanup_max_age_hours is configurable.
"""

import tempfile
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


class TestCleanupWiring:
    """Test cleanup wiring to SQLite backend (Components 5-7)."""

    def setup_method(self):
        """Setup test environment with SQLite backend."""
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

    def _create_manager(self, cleanup_max_age_hours=24):
        """Create a manager with the specified cleanup config."""
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
                cleanup_max_age_hours=cleanup_max_age_hours,
            ),
        )
        return self.manager

    def test_cleanup_old_jobs_calls_sqlite_backend(self):
        """Component 5: cleanup_old_jobs() should call sqlite_backend.cleanup_old_jobs().

        After cleanup, old jobs should be gone from BOTH memory AND SQLite.
        """
        manager = self._create_manager()

        # Add old completed jobs to memory and SQLite
        old_time = datetime.now(timezone.utc) - timedelta(hours=48)
        for i in range(5):
            jid = f"old-{i}"
            manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="test_op",
                status=JobStatus.COMPLETED,
                created_at=old_time,
                started_at=old_time,
                completed_at=old_time,
                result=None,
                error=None,
                progress=100,
                username="testuser",
            )
            manager._sqlite_backend.save_job(
                job_id=jid,
                operation_type="test_op",
                status="completed",
                created_at=old_time.isoformat(),
                started_at=old_time.isoformat(),
                completed_at=old_time.isoformat(),
                username="testuser",
                progress=100,
            )

        # Act: Cleanup
        manager.cleanup_old_jobs(max_age_hours=24)

        # Assert: Jobs removed from memory
        assert len(manager.jobs) == 0, f"Expected 0 jobs in memory, got {len(manager.jobs)}"

        # Assert: Jobs removed from SQLite too
        remaining_db_jobs = manager._sqlite_backend.list_jobs(limit=100)
        old_db_jobs = [j for j in remaining_db_jobs if j["job_id"].startswith("old-")]
        assert len(old_db_jobs) == 0, (
            f"Expected 0 old jobs in SQLite, got {len(old_db_jobs)}"
        )

    def test_cleanup_old_jobs_removes_from_memory_and_db(self):
        """Component 5: Cleanup should remove from both memory AND DB, preserving recent jobs."""
        manager = self._create_manager()

        old_time = datetime.now(timezone.utc) - timedelta(hours=48)
        recent_time = datetime.now(timezone.utc) - timedelta(hours=1)

        # Add old jobs
        for i in range(3):
            jid = f"old-{i}"
            manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="test_op",
                status=JobStatus.COMPLETED,
                created_at=old_time,
                started_at=old_time,
                completed_at=old_time,
                result=None,
                error=None,
                progress=100,
                username="testuser",
            )
            manager._sqlite_backend.save_job(
                job_id=jid,
                operation_type="test_op",
                status="completed",
                created_at=old_time.isoformat(),
                completed_at=old_time.isoformat(),
                username="testuser",
                progress=100,
            )

        # Add recent jobs
        for i in range(2):
            jid = f"recent-{i}"
            manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="test_op",
                status=JobStatus.COMPLETED,
                created_at=recent_time,
                started_at=recent_time,
                completed_at=recent_time,
                result=None,
                error=None,
                progress=100,
                username="testuser",
            )
            manager._sqlite_backend.save_job(
                job_id=jid,
                operation_type="test_op",
                status="completed",
                created_at=recent_time.isoformat(),
                completed_at=recent_time.isoformat(),
                username="testuser",
                progress=100,
            )

        # Act
        cleaned = manager.cleanup_old_jobs(max_age_hours=24)

        # Assert: Old jobs removed, recent preserved
        assert cleaned == 3, f"Expected 3 cleaned, got {cleaned}"
        assert len(manager.jobs) == 2, f"Expected 2 jobs in memory, got {len(manager.jobs)}"
        for i in range(2):
            assert f"recent-{i}" in manager.jobs

        # Verify SQLite
        db_jobs = manager._sqlite_backend.list_jobs(limit=100)
        db_job_ids = [j["job_id"] for j in db_jobs]
        for i in range(2):
            assert f"recent-{i}" in db_job_ids
        for i in range(3):
            assert f"old-{i}" not in db_job_ids

    def test_startup_cleanup_runs_before_load(self):
        """Component 6: On startup, old jobs should be cleaned from SQLite before loading.

        Insert old completed jobs directly into SQLite, then create a new manager.
        The manager should clean them up during initialization.
        """
        # Create initial manager and add old jobs to SQLite
        manager1 = self._create_manager(cleanup_max_age_hours=24)
        old_time = datetime.now(timezone.utc) - timedelta(hours=48)

        for i in range(50):
            manager1._sqlite_backend.save_job(
                job_id=f"stale-{i}",
                operation_type="test_op",
                status="completed",
                created_at=old_time.isoformat(),
                completed_at=old_time.isoformat(),
                username="testuser",
                progress=100,
            )

        # Add a recent job that should survive cleanup
        recent_time = datetime.now(timezone.utc) - timedelta(hours=1)
        manager1._sqlite_backend.save_job(
            job_id="recent-survivor",
            operation_type="test_op",
            status="completed",
            created_at=recent_time.isoformat(),
            completed_at=recent_time.isoformat(),
            username="testuser",
            progress=100,
        )

        manager1.shutdown()
        self.manager = None  # Prevent double shutdown in teardown

        # Act: Create new manager - should cleanup old jobs during init
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
                cleanup_max_age_hours=24,
            ),
        )

        # Assert: Old jobs should be cleaned from SQLite
        all_db_jobs = self.manager._sqlite_backend.list_jobs(limit=1000)
        stale_jobs = [j for j in all_db_jobs if j["job_id"].startswith("stale-")]
        assert len(stale_jobs) == 0, (
            f"Expected 0 stale jobs after startup cleanup, got {len(stale_jobs)}"
        )

    def test_cleanup_preserves_running_jobs(self):
        """Component 5-6: Cleanup should never remove running/pending jobs."""
        manager = self._create_manager()

        old_time = datetime.now(timezone.utc) - timedelta(hours=48)

        # Add an old running job
        manager.jobs["old-running"] = BackgroundJob(
            job_id="old-running",
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=old_time,
            started_at=old_time,
            completed_at=None,
            result=None,
            error=None,
            progress=50,
            username="testuser",
        )
        manager._sqlite_backend.save_job(
            job_id="old-running",
            operation_type="test_op",
            status="running",
            created_at=old_time.isoformat(),
            started_at=old_time.isoformat(),
            username="testuser",
            progress=50,
        )

        # Add an old pending job
        manager.jobs["old-pending"] = BackgroundJob(
            job_id="old-pending",
            operation_type="test_op",
            status=JobStatus.PENDING,
            created_at=old_time,
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="testuser",
        )
        manager._sqlite_backend.save_job(
            job_id="old-pending",
            operation_type="test_op",
            status="pending",
            created_at=old_time.isoformat(),
            username="testuser",
            progress=0,
        )

        # Add an old completed job (should be cleaned)
        manager.jobs["old-completed"] = BackgroundJob(
            job_id="old-completed",
            operation_type="test_op",
            status=JobStatus.COMPLETED,
            created_at=old_time,
            started_at=old_time,
            completed_at=old_time,
            result=None,
            error=None,
            progress=100,
            username="testuser",
        )
        manager._sqlite_backend.save_job(
            job_id="old-completed",
            operation_type="test_op",
            status="completed",
            created_at=old_time.isoformat(),
            completed_at=old_time.isoformat(),
            username="testuser",
            progress=100,
        )

        # Act
        cleaned = manager.cleanup_old_jobs(max_age_hours=24)

        # Assert: Only the completed job was cleaned
        assert cleaned == 1, f"Expected 1 cleaned, got {cleaned}"
        assert "old-running" in manager.jobs, "Running job should be preserved"
        assert "old-pending" in manager.jobs, "Pending job should be preserved"
        assert "old-completed" not in manager.jobs, "Completed job should be cleaned"
