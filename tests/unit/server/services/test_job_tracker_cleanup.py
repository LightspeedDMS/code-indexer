"""
Unit tests for JobTracker.cleanup_orphaned_jobs_on_startup.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC6: cleanup_orphaned_jobs_on_startup
"""

import sqlite3
from datetime import datetime, timezone

from code_indexer.server.services.job_tracker import JobTracker


def _insert_job_directly(db_path: str, job_id: str, status: str) -> None:
    """Insert a row directly into SQLite, bypassing JobTracker (simulates pre-restart state)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO background_jobs
           (job_id, operation_type, status, created_at, progress, username,
            is_admin, cancelled, resolution_attempts)
           VALUES (?, 'test_op', ?, ?, 0, 'admin', 0, 0, 0)""",
        (job_id, status, now_iso),
    )
    conn.commit()
    conn.close()


class TestCleanupOrphanedJobs:
    """Tests for cleanup_orphaned_jobs_on_startup (AC6)."""

    def test_cleanup_marks_running_as_failed(self, db_path):
        """
        cleanup_orphaned_jobs_on_startup marks running jobs as failed.

        Given a 'running' job in SQLite (simulating a pre-restart state)
        When cleanup_orphaned_jobs_on_startup is called on a fresh tracker
        Then the job status becomes 'failed'
        """
        _insert_job_directly(db_path, "orphan-run-001", "running")

        fresh_tracker = JobTracker(db_path)
        fresh_tracker.cleanup_orphaned_jobs_on_startup()

        job = fresh_tracker.get_job("orphan-run-001")
        assert job is not None
        assert job.status == "failed"

    def test_cleanup_marks_pending_as_failed(self, db_path):
        """
        cleanup_orphaned_jobs_on_startup marks pending jobs as failed.

        Given a 'pending' job in SQLite (simulating a pre-restart state)
        When cleanup_orphaned_jobs_on_startup is called on a fresh tracker
        Then the job status becomes 'failed'
        """
        _insert_job_directly(db_path, "orphan-pend-001", "pending")

        fresh_tracker = JobTracker(db_path)
        fresh_tracker.cleanup_orphaned_jobs_on_startup()

        job = fresh_tracker.get_job("orphan-pend-001")
        assert job is not None
        assert job.status == "failed"

    def test_cleanup_sets_orphan_error_message(self, db_path):
        """
        cleanup_orphaned_jobs_on_startup sets error='orphaned - server restarted'.

        Given orphaned running/pending jobs
        When cleanup is called
        Then the error field on each job is 'orphaned - server restarted'
        """
        _insert_job_directly(db_path, "orphan-err-001", "running")

        fresh_tracker = JobTracker(db_path)
        fresh_tracker.cleanup_orphaned_jobs_on_startup()

        job = fresh_tracker.get_job("orphan-err-001")
        assert job is not None
        assert job.error == "orphaned - server restarted"

    def test_cleanup_ignores_completed_jobs(self, db_path):
        """
        cleanup_orphaned_jobs_on_startup does not alter completed jobs.

        Given a completed job in SQLite
        When cleanup is called
        Then the completed job remains completed
        """
        _insert_job_directly(db_path, "completed-job-001", "completed")

        fresh_tracker = JobTracker(db_path)
        fresh_tracker.cleanup_orphaned_jobs_on_startup()

        job = fresh_tracker.get_job("completed-job-001")
        assert job is not None
        assert job.status == "completed"

    def test_cleanup_returns_count(self, db_path):
        """
        cleanup_orphaned_jobs_on_startup returns the number of orphaned jobs.

        Given two orphaned jobs (one running, one pending) and one completed job
        When cleanup is called
        Then the return value is 2
        """
        _insert_job_directly(db_path, "orphan-cnt-001", "running")
        _insert_job_directly(db_path, "orphan-cnt-002", "pending")
        _insert_job_directly(db_path, "orphan-cnt-003", "completed")

        fresh_tracker = JobTracker(db_path)
        count = fresh_tracker.cleanup_orphaned_jobs_on_startup()

        assert count == 2
