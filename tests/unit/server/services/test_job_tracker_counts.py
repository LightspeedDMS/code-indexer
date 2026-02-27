"""
Unit tests for JobTracker job counts and thread safety.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC1 (thread safety) and AC3 (get_active_job_count, get_pending_job_count)
"""

import threading

import pytest



class TestJobTrackerCounts:
    """Tests for get_active_job_count and get_pending_job_count."""

    def test_get_active_job_count(self, tracker):
        """
        get_active_job_count returns the number of in-memory jobs with status='running'.

        Given two running jobs and one pending job
        When get_active_job_count is called
        Then the count is 2
        """
        tracker.register_job("job-cnt-run-001", "op_a", "admin")
        tracker.register_job("job-cnt-run-002", "op_b", "admin")
        tracker.register_job("job-cnt-pend-001", "op_c", "admin")
        tracker.update_status("job-cnt-run-001", status="running")
        tracker.update_status("job-cnt-run-002", status="running")

        assert tracker.get_active_job_count() == 2

    def test_get_pending_job_count(self, tracker):
        """
        get_pending_job_count returns the number of in-memory jobs with status='pending'.

        Given two pending jobs and one running job
        When get_pending_job_count is called
        Then the count is 2
        """
        tracker.register_job("job-pcnt-001", "op_a", "admin")
        tracker.register_job("job-pcnt-002", "op_b", "admin")
        tracker.register_job("job-pcnt-run-001", "op_c", "admin")
        tracker.update_status("job-pcnt-run-001", status="running")

        assert tracker.get_pending_job_count() == 2

    def test_counts_after_completion(self, tracker):
        """
        Counts decrease to 0 after all jobs are completed.

        Given two running jobs
        When both are completed
        Then get_active_job_count returns 0
        """
        tracker.register_job("job-cnt-comp-001", "op_a", "admin")
        tracker.register_job("job-cnt-comp-002", "op_b", "admin")
        tracker.update_status("job-cnt-comp-001", status="running")
        tracker.update_status("job-cnt-comp-002", status="running")

        tracker.complete_job("job-cnt-comp-001")
        tracker.complete_job("job-cnt-comp-002")

        assert tracker.get_active_job_count() == 0


class TestJobTrackerThreadSafety:
    """Thread safety tests for JobTracker concurrent access."""

    def test_concurrent_register_and_complete(self, tracker):
        """
        Concurrent register, update, and complete operations from 10 threads
        complete without errors or data corruption.

        Given 10 threads each registering, updating, and completing a unique job
        When all threads run concurrently
        Then all 10 jobs end up in SQLite with status='completed', no exceptions
        """
        errors = []
        job_ids = [f"thread-job-{i:03d}" for i in range(10)]

        def worker(job_id: str) -> None:
            try:
                tracker.register_job(job_id, "concurrent_op", "admin")
                tracker.update_status(job_id, status="running", progress=50)
                tracker.complete_job(job_id)
            except Exception as exc:
                errors.append((job_id, str(exc)))

        threads = [threading.Thread(target=worker, args=(jid,)) for jid in job_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"

        for job_id in job_ids:
            job = tracker.get_job(job_id)
            assert job is not None, f"Job {job_id} missing from SQLite"
            assert job.status == "completed", f"Job {job_id} status={job.status}"
