"""
Unit tests for JobTracker.register_job and JobTracker.update_status.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC1: register_job, update_status
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from code_indexer.server.services.job_tracker import JobTracker, TrackedJob


class TestJobTrackerRegister:
    """Tests for register_job (AC1)."""

    def test_register_job_returns_tracked_job(self, tracker):
        """
        register_job returns a TrackedJob instance with correct field values.

        Given a new job_id and operation_type
        When register_job is called
        Then a TrackedJob with matching fields is returned
        """
        job = tracker.register_job(
            job_id="job-001",
            operation_type="dep_map_analysis",
            username="admin",
        )

        assert isinstance(job, TrackedJob)
        assert job.job_id == "job-001"
        assert job.operation_type == "dep_map_analysis"
        assert job.username == "admin"

    def test_register_job_sets_pending_status(self, tracker):
        """
        register_job always creates job with status 'pending'.

        Given a new job registration
        When register_job is called
        Then the returned job has status='pending'
        """
        job = tracker.register_job(
            job_id="job-pending-001",
            operation_type="test_op",
            username="user1",
        )

        assert job.status == "pending"

    def test_register_job_persists_to_sqlite(self, tracker, db_path):
        """
        register_job persists the job row to SQLite immediately.

        Given a registered job
        When querying the SQLite database directly
        Then a row with matching job_id exists
        """
        tracker.register_job(
            job_id="job-persist-001",
            operation_type="dep_map_analysis",
            username="admin",
        )

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT job_id, status FROM background_jobs WHERE job_id = ?",
            ("job-persist-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "job-persist-001"
        assert row[1] == "pending"

    def test_register_job_in_memory(self, tracker):
        """
        register_job adds the job to the in-memory active jobs.

        Given a registered job
        When get_active_jobs is called
        Then the new job appears in the list
        """
        tracker.register_job(
            job_id="job-mem-001",
            operation_type="dep_map_analysis",
            username="admin",
        )

        active = tracker.get_active_jobs()
        ids = [j.job_id for j in active]
        assert "job-mem-001" in ids

    def test_register_job_with_metadata(self, tracker, db_path):
        """
        register_job stores metadata dict as JSON in SQLite.

        Given metadata dict is provided
        When register_job is called
        Then the SQLite row has the metadata stored as JSON
        """
        metadata = {"source": "test", "version": 3}
        tracker.register_job(
            job_id="job-meta-001",
            operation_type="test_op",
            username="admin",
            metadata=metadata,
        )

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT metadata FROM background_jobs WHERE job_id = ?",
            ("job-meta-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        stored_meta = json.loads(row[0])
        assert stored_meta == metadata

    def test_register_job_with_repo_alias(self, tracker):
        """
        register_job stores repo_alias correctly on the returned TrackedJob.

        Given a repo_alias is provided
        When register_job is called
        Then the returned job has the correct repo_alias
        """
        job = tracker.register_job(
            job_id="job-alias-001",
            operation_type="dep_map_analysis",
            username="admin",
            repo_alias="my-repo",
        )

        assert job.repo_alias == "my-repo"


class TestJobTrackerUpdateStatus:
    """Tests for update_status (AC1)."""

    def test_update_status_changes_status(self, tracker):
        """
        update_status changes the in-memory job status.

        Given a pending job
        When update_status is called with status='running'
        Then the job status becomes 'running'
        """
        tracker.register_job("job-upd-001", "dep_map_analysis", "admin")
        tracker.update_status("job-upd-001", status="running")

        job = tracker.get_job("job-upd-001")
        assert job is not None
        assert job.status == "running"

    def test_update_status_sets_started_at_on_running(self, tracker):
        """
        update_status sets started_at automatically when status changes to 'running'.

        Given a pending job with started_at=None
        When update_status(status='running') is called
        Then started_at is set to a recent datetime
        """
        before = datetime.now(timezone.utc)
        tracker.register_job("job-start-001", "dep_map_analysis", "admin")
        tracker.update_status("job-start-001", status="running")
        after = datetime.now(timezone.utc)

        job = tracker.get_job("job-start-001")
        assert job is not None
        assert job.started_at is not None
        assert before <= job.started_at <= after

    def test_update_status_does_not_reset_started_at(self, tracker):
        """
        A second call to update_status with status='running' does not overwrite started_at.

        Given a running job with started_at already set
        When update_status(status='running') is called again
        Then started_at remains unchanged
        """
        tracker.register_job("job-noReset-001", "dep_map_analysis", "admin")
        tracker.update_status("job-noReset-001", status="running")

        job = tracker.get_job("job-noReset-001")
        assert job is not None
        original_started = job.started_at

        time.sleep(0.01)
        tracker.update_status("job-noReset-001", status="running")

        job2 = tracker.get_job("job-noReset-001")
        assert job2 is not None
        assert job2.started_at == original_started

    def test_update_status_updates_progress(self, tracker):
        """
        update_status updates the progress field.

        Given an active job
        When update_status(progress=50) is called
        Then the job's progress becomes 50
        """
        tracker.register_job("job-prog-001", "dep_map_analysis", "admin")
        tracker.update_status("job-prog-001", status="running", progress=50)

        job = tracker.get_job("job-prog-001")
        assert job is not None
        assert job.progress == 50

    def test_update_status_updates_progress_info(self, tracker):
        """
        update_status updates the progress_info field.

        Given an active job
        When update_status(progress_info='Pass 2/3') is called
        Then the job's progress_info is 'Pass 2/3'
        """
        tracker.register_job("job-pinfo-001", "dep_map_analysis", "admin")
        tracker.update_status("job-pinfo-001", status="running", progress_info="Pass 2/3")

        job = tracker.get_job("job-pinfo-001")
        assert job is not None
        assert job.progress_info == "Pass 2/3"

    def test_update_status_persists_to_sqlite(self, tracker, db_path):
        """
        update_status persists changes to SQLite.

        Given a job is updated to running with progress=75
        When the SQLite database is queried directly
        Then the row reflects the updated values
        """
        tracker.register_job("job-sqlPersist-001", "dep_map_analysis", "admin")
        tracker.update_status("job-sqlPersist-001", status="running", progress=75)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, progress FROM background_jobs WHERE job_id = ?",
            ("job-sqlPersist-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "running"
        assert row[1] == 75

    def test_update_status_ignores_missing_job(self, tracker):
        """
        update_status silently ignores calls for unknown job_id.

        Given a job_id that was never registered
        When update_status is called
        Then no exception is raised
        """
        tracker.update_status("nonexistent-job", status="running")
