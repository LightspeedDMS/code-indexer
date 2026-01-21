"""
Unit tests for Bug Fix: Running/pending jobs show "Unknown" as repository name.

Tests that get_recent_jobs_with_filter() includes repo_alias in the returned
dictionary so that running/pending jobs can display their repository name
before the job completes and populates the result dictionary.

Root Cause:
- get_recent_jobs_with_filter() did NOT include repo_alias in returned dict
- Dashboard service only checked result dict for repo name
- result is empty for running/pending jobs => "Unknown"

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)


class TestRepoAliasInRecentJobs:
    """Test that repo_alias is included in get_recent_jobs_with_filter() output."""

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

    def test_running_job_includes_repo_alias_in_dict(self):
        """Test that running jobs include repo_alias in the returned dictionary.

        This is the core bug fix: running jobs should have repo_alias available
        in the job dict so the dashboard can display the repository name even
        when the job hasn't completed yet (and result is empty).
        """
        now = datetime.now(timezone.utc)
        running_job = BackgroundJob(
            job_id="running-job-with-alias",
            operation_type="add_golden_repo",
            status=JobStatus.RUNNING,
            created_at=now - timedelta(minutes=5),
            started_at=now - timedelta(minutes=4),
            completed_at=None,
            result=None,  # Running jobs have empty result
            error=None,
            progress=50,
            username="testuser",
            repo_alias="my-test-repo",  # This should be in output
        )

        with self.manager._lock:
            self.manager.jobs["running-job-with-alias"] = running_job

        recent_jobs = self.manager.get_recent_jobs_with_filter(time_filter="24h")

        assert len(recent_jobs) == 1
        job_dict = recent_jobs[0]
        assert job_dict["job_id"] == "running-job-with-alias"
        assert job_dict["status"] == "running"
        # THE BUG FIX: repo_alias must be in the dictionary
        assert "repo_alias" in job_dict, "repo_alias must be included in job dict"
        assert job_dict["repo_alias"] == "my-test-repo"

    def test_pending_job_includes_repo_alias_in_dict(self):
        """Test that pending jobs include repo_alias in the returned dictionary.

        Pending jobs also need repo_alias to display the repository name.
        """
        now = datetime.now(timezone.utc)
        pending_job = BackgroundJob(
            job_id="pending-job-with-alias",
            operation_type="refresh_golden_repo",
            status=JobStatus.PENDING,
            created_at=now - timedelta(minutes=2),
            started_at=None,
            completed_at=None,
            result=None,  # Pending jobs have empty result
            error=None,
            progress=0,
            username="testuser",
            repo_alias="another-repo",  # This should be in output
        )

        with self.manager._lock:
            self.manager.jobs["pending-job-with-alias"] = pending_job

        recent_jobs = self.manager.get_recent_jobs_with_filter(time_filter="24h")

        assert len(recent_jobs) == 1
        job_dict = recent_jobs[0]
        assert job_dict["job_id"] == "pending-job-with-alias"
        assert job_dict["status"] == "pending"
        # THE BUG FIX: repo_alias must be in the dictionary
        assert "repo_alias" in job_dict, "repo_alias must be included in job dict"
        assert job_dict["repo_alias"] == "another-repo"

    def test_completed_job_includes_repo_alias_in_dict(self):
        """Test that completed jobs also include repo_alias for consistency.

        Even though completed jobs have result dict with alias, repo_alias
        should also be present for API consistency.
        """
        now = datetime.now(timezone.utc)
        completed_job = BackgroundJob(
            job_id="completed-job-with-alias",
            operation_type="add_golden_repo",
            status=JobStatus.COMPLETED,
            created_at=now - timedelta(minutes=10),
            started_at=now - timedelta(minutes=9),
            completed_at=now - timedelta(minutes=1),
            result={"alias": "completed-repo"},  # Completed jobs have result
            error=None,
            progress=100,
            username="testuser",
            repo_alias="completed-repo",  # Should also be in output
        )

        with self.manager._lock:
            self.manager.jobs["completed-job-with-alias"] = completed_job

        recent_jobs = self.manager.get_recent_jobs_with_filter(time_filter="24h")

        assert len(recent_jobs) == 1
        job_dict = recent_jobs[0]
        assert job_dict["job_id"] == "completed-job-with-alias"
        assert "repo_alias" in job_dict, "repo_alias must be included in job dict"
        assert job_dict["repo_alias"] == "completed-repo"

    def test_job_without_repo_alias_returns_none(self):
        """Test that jobs without repo_alias have None in the dict.

        Some jobs may not have a repo_alias set. The dict should still
        include the key with None value for consistency.
        """
        now = datetime.now(timezone.utc)
        job_without_alias = BackgroundJob(
            job_id="job-no-alias",
            operation_type="some_operation",
            status=JobStatus.RUNNING,
            created_at=now - timedelta(minutes=3),
            started_at=now - timedelta(minutes=2),
            completed_at=None,
            result=None,
            error=None,
            progress=25,
            username="testuser",
            repo_alias=None,  # No alias set
        )

        with self.manager._lock:
            self.manager.jobs["job-no-alias"] = job_without_alias

        recent_jobs = self.manager.get_recent_jobs_with_filter(time_filter="24h")

        assert len(recent_jobs) == 1
        job_dict = recent_jobs[0]
        assert "repo_alias" in job_dict, "repo_alias key must exist even if None"
        assert job_dict["repo_alias"] is None
