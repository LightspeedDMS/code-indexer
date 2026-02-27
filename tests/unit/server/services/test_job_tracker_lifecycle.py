"""
Unit tests for JobTracker.complete_job and JobTracker.fail_job.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC1: complete_job, fail_job
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest



class TestJobTrackerComplete:
    """Tests for complete_job (AC1)."""

    def test_complete_job_sets_completed_status(self, tracker):
        """
        complete_job sets the job status to 'completed' in SQLite.

        Given a running job
        When complete_job is called
        Then the job status is 'completed'
        """
        tracker.register_job("job-comp-001", "dep_map_analysis", "admin")
        tracker.update_status("job-comp-001", status="running")
        tracker.complete_job("job-comp-001")

        job = tracker.get_job("job-comp-001")
        assert job is not None
        assert job.status == "completed"

    def test_complete_job_sets_completed_at(self, tracker):
        """
        complete_job sets completed_at to a recent UTC datetime.

        Given a running job
        When complete_job is called
        Then completed_at is set to a recent timestamp
        """
        before = datetime.now(timezone.utc)
        tracker.register_job("job-compAt-001", "dep_map_analysis", "admin")
        tracker.update_status("job-compAt-001", status="running")
        tracker.complete_job("job-compAt-001")
        after = datetime.now(timezone.utc)

        job = tracker.get_job("job-compAt-001")
        assert job is not None
        assert job.completed_at is not None
        assert before <= job.completed_at <= after

    def test_complete_job_removes_from_memory(self, tracker):
        """
        complete_job removes the job from the in-memory active jobs dict.

        Given a running job
        When complete_job is called
        Then the job no longer appears in get_active_jobs()
        """
        tracker.register_job("job-remMem-001", "dep_map_analysis", "admin")
        tracker.update_status("job-remMem-001", status="running")
        tracker.complete_job("job-remMem-001")

        active_ids = [j.job_id for j in tracker.get_active_jobs()]
        assert "job-remMem-001" not in active_ids

    def test_complete_job_persists_to_sqlite(self, tracker, db_path):
        """
        complete_job persists the completed state to SQLite.

        Given a running job that is completed
        When querying SQLite directly
        Then the row shows status='completed'
        """
        tracker.register_job("job-sqlComp-001", "dep_map_analysis", "admin")
        tracker.update_status("job-sqlComp-001", status="running")
        tracker.complete_job("job-sqlComp-001")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?",
            ("job-sqlComp-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "completed"

    def test_complete_job_with_result(self, tracker, db_path):
        """
        complete_job stores the result dict as JSON in SQLite.

        Given a result dict is provided
        When complete_job is called
        Then the result is stored as JSON in SQLite
        """
        result = {"total_files": 42, "duration_ms": 1234}
        tracker.register_job("job-result-001", "dep_map_analysis", "admin")
        tracker.update_status("job-result-001", status="running")
        tracker.complete_job("job-result-001", result=result)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT result FROM background_jobs WHERE job_id = ?",
            ("job-result-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        stored_result = json.loads(row[0])
        assert stored_result == result

    def test_complete_job_ignores_missing(self, tracker):
        """
        complete_job silently ignores unknown job_id.

        Given a job_id that does not exist in memory
        When complete_job is called
        Then no exception is raised
        """
        tracker.complete_job("nonexistent-job")


class TestJobTrackerFail:
    """Tests for fail_job (AC1)."""

    def test_fail_job_sets_failed_status(self, tracker):
        """
        fail_job sets job status to 'failed'.

        Given a running job
        When fail_job is called
        Then the job status is 'failed'
        """
        tracker.register_job("job-fail-001", "dep_map_analysis", "admin")
        tracker.update_status("job-fail-001", status="running")
        tracker.fail_job("job-fail-001", error="Something went wrong")

        job = tracker.get_job("job-fail-001")
        assert job is not None
        assert job.status == "failed"

    def test_fail_job_sets_error_message(self, tracker):
        """
        fail_job stores the error message on the job.

        Given a running job
        When fail_job(error='Timeout') is called
        Then the job's error field is 'Timeout'
        """
        tracker.register_job("job-errMsg-001", "dep_map_analysis", "admin")
        tracker.update_status("job-errMsg-001", status="running")
        tracker.fail_job("job-errMsg-001", error="Timeout")

        job = tracker.get_job("job-errMsg-001")
        assert job is not None
        assert job.error == "Timeout"

    def test_fail_job_removes_from_memory(self, tracker):
        """
        fail_job removes the job from the in-memory active jobs dict.

        Given a running job
        When fail_job is called
        Then the job no longer appears in get_active_jobs()
        """
        tracker.register_job("job-failMem-001", "dep_map_analysis", "admin")
        tracker.update_status("job-failMem-001", status="running")
        tracker.fail_job("job-failMem-001", error="Error")

        active_ids = [j.job_id for j in tracker.get_active_jobs()]
        assert "job-failMem-001" not in active_ids

    def test_fail_job_persists_to_sqlite(self, tracker, db_path):
        """
        fail_job persists the failed state to SQLite.

        Given a job that has been failed
        When querying SQLite directly
        Then the row shows status='failed' and the error message
        """
        tracker.register_job("job-sqlFail-001", "dep_map_analysis", "admin")
        tracker.update_status("job-sqlFail-001", status="running")
        tracker.fail_job("job-sqlFail-001", error="DB connection lost")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status, error FROM background_jobs WHERE job_id = ?",
            ("job-sqlFail-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "failed"
        assert row[1] == "DB connection lost"

    def test_fail_job_ignores_missing(self, tracker):
        """
        fail_job silently ignores unknown job_id.

        Given a job_id that does not exist in memory
        When fail_job is called
        Then no exception is raised
        """
        tracker.fail_job("nonexistent-job", error="irrelevant")
