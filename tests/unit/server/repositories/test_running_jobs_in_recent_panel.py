"""
Unit tests for Story #4 AC1: Running Jobs in Recent Jobs Panel.

Tests that get_recent_jobs_with_filter() includes RUNNING and PENDING jobs,
with running jobs appearing at the top of the list before completed jobs.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)


class TestRunningJobsInRecentPanel:
    """Test AC1: Running jobs appear in Recent Jobs panel."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.job_storage_path = Path(self.temp_dir) / "jobs.json"
        self.manager = BackgroundJobManager(storage_path=str(self.job_storage_path))

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        import shutil
        import os

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_get_recent_jobs_includes_running_status(self):
        """Test that running jobs are included in get_recent_jobs_with_filter."""
        now = datetime.now(timezone.utc)
        running_job = BackgroundJob(
            job_id="running-job-1",
            operation_type="add_golden_repo",
            status=JobStatus.RUNNING,
            created_at=now - timedelta(minutes=5),
            started_at=now - timedelta(minutes=4),
            completed_at=None,
            result=None,
            error=None,
            progress=50,
            username="testuser",
        )

        with self.manager._lock:
            self.manager.jobs["running-job-1"] = running_job

        recent_jobs = self.manager.get_recent_jobs_with_filter(time_filter="24h")

        assert len(recent_jobs) == 1
        assert recent_jobs[0]["job_id"] == "running-job-1"
        assert recent_jobs[0]["status"] == "running"

    def test_get_recent_jobs_includes_pending_status(self):
        """Test that pending jobs are included in get_recent_jobs_with_filter."""
        now = datetime.now(timezone.utc)
        pending_job = BackgroundJob(
            job_id="pending-job-1",
            operation_type="refresh_golden_repo",
            status=JobStatus.PENDING,
            created_at=now - timedelta(minutes=2),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="testuser",
        )

        with self.manager._lock:
            self.manager.jobs["pending-job-1"] = pending_job

        recent_jobs = self.manager.get_recent_jobs_with_filter(time_filter="24h")

        assert len(recent_jobs) == 1
        assert recent_jobs[0]["job_id"] == "pending-job-1"
        assert recent_jobs[0]["status"] == "pending"

    def test_running_jobs_appear_before_completed_jobs(self):
        """Test that running jobs appear at the top of the list."""
        now = datetime.now(timezone.utc)

        completed_job = BackgroundJob(
            job_id="completed-job-1",
            operation_type="add_golden_repo",
            status=JobStatus.COMPLETED,
            created_at=now - timedelta(minutes=10),
            started_at=now - timedelta(minutes=9),
            completed_at=now - timedelta(minutes=1),
            result={"alias": "test-repo"},
            error=None,
            progress=100,
            username="testuser",
        )

        running_job = BackgroundJob(
            job_id="running-job-1",
            operation_type="refresh_golden_repo",
            status=JobStatus.RUNNING,
            created_at=now - timedelta(minutes=5),
            started_at=now - timedelta(minutes=4),
            completed_at=None,
            result=None,
            error=None,
            progress=50,
            username="testuser",
        )

        with self.manager._lock:
            self.manager.jobs["completed-job-1"] = completed_job
            self.manager.jobs["running-job-1"] = running_job

        recent_jobs = self.manager.get_recent_jobs_with_filter(time_filter="24h")

        assert len(recent_jobs) == 2
        assert recent_jobs[0]["status"] == "running", "Running jobs should be at top"
        assert recent_jobs[1]["status"] == "completed"
