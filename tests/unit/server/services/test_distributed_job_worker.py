"""Tests for DistributedJobWorkerService (Bug #582)."""

import time

import pytest

from code_indexer.server.services.distributed_job_worker import (
    DistributedJobWorkerService,
)


class FakeClaimer:
    """Fake DistributedJobClaimer that returns pre-configured jobs."""

    def __init__(self):
        self.jobs_to_return = []
        self.completed_jobs = []
        self.failed_jobs = []

    def claim_next_job(self):
        if self.jobs_to_return:
            return self.jobs_to_return.pop(0)
        return None

    def complete_job(self, job_id, result=None):
        self.completed_jobs.append((job_id, result))
        return True

    def fail_job(self, job_id, error):
        self.failed_jobs.append((job_id, error))
        return True


class FakeRefreshScheduler:
    """Fake RefreshScheduler that records trigger_refresh_for_repo calls."""

    def __init__(self):
        self.refreshed_repos = []

    def trigger_refresh_for_repo(self, alias, submitter_username="system"):
        self.refreshed_repos.append((alias, submitter_username))
        return "fake-job-id"


class TestProcessOneJob:
    """Tests for _process_one_job logic."""

    def test_no_pending_jobs_noop(self):
        """When claimer returns None, no action is taken."""
        claimer = FakeClaimer()
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
        )

        worker._process_one_job()

        assert len(claimer.completed_jobs) == 0
        assert len(claimer.failed_jobs) == 0
        assert len(scheduler.refreshed_repos) == 0

    def test_process_retryable_job_calls_refresh(self):
        """Claiming a global_repo_refresh job triggers refresh and completes."""
        claimer = FakeClaimer()
        claimer.jobs_to_return.append(
            {
                "job_id": "job-123",
                "operation_type": "global_repo_refresh",
                "repo_alias": "my-repo-global",
            }
        )
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
        )

        worker._process_one_job()

        assert len(scheduler.refreshed_repos) == 1
        assert scheduler.refreshed_repos[0] == ("my-repo-global", "system")
        assert len(claimer.completed_jobs) == 1
        assert claimer.completed_jobs[0][0] == "job-123"
        assert len(claimer.failed_jobs) == 0

    def test_non_retryable_job_marked_failed(self):
        """Unknown operation_type is marked as failed immediately."""
        claimer = FakeClaimer()
        claimer.jobs_to_return.append(
            {
                "job_id": "job-456",
                "operation_type": "unknown_op",
                "repo_alias": "some-repo",
            }
        )
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
        )

        worker._process_one_job()

        assert len(claimer.failed_jobs) == 1
        assert claimer.failed_jobs[0][0] == "job-456"
        assert "Non-retryable" in claimer.failed_jobs[0][1]
        assert len(claimer.completed_jobs) == 0
        assert len(scheduler.refreshed_repos) == 0

    def test_execution_error_marks_failed(self):
        """Exception during refresh execution marks job as failed."""
        claimer = FakeClaimer()
        claimer.jobs_to_return.append(
            {
                "job_id": "job-789",
                "operation_type": "global_repo_refresh",
                "repo_alias": "bad-repo-global",
            }
        )

        class FailingScheduler:
            def trigger_refresh_for_repo(self, alias, submitter_username="system"):
                raise RuntimeError("git pull failed")

        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=FailingScheduler(),
        )

        worker._process_one_job()

        assert len(claimer.failed_jobs) == 1
        assert claimer.failed_jobs[0][0] == "job-789"
        assert "git pull failed" in claimer.failed_jobs[0][1]
        assert len(claimer.completed_jobs) == 0

    def test_missing_repo_alias_marks_failed(self):
        """Refresh job without repo_alias is marked as failed."""
        claimer = FakeClaimer()
        claimer.jobs_to_return.append(
            {
                "job_id": "job-no-alias",
                "operation_type": "refresh_golden_repo",
                "repo_alias": "",
            }
        )
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
        )

        worker._process_one_job()

        assert len(claimer.failed_jobs) == 1
        assert claimer.failed_jobs[0][0] == "job-no-alias"
        assert "repo_alias is required" in claimer.failed_jobs[0][1]
        assert len(claimer.completed_jobs) == 0
        assert len(scheduler.refreshed_repos) == 0


class TestStartStop:
    """Tests for worker thread lifecycle."""

    def test_start_stop(self):
        """Worker thread starts and stops cleanly."""
        claimer = FakeClaimer()
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
            poll_interval=1,
        )

        worker.start()
        assert worker._thread is not None
        assert worker._thread.is_alive()

        worker.stop()
        assert not worker._thread.is_alive()

    def test_start_is_idempotent(self):
        """Calling start twice does not create a second thread."""
        claimer = FakeClaimer()
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
            poll_interval=1,
        )

        worker.start()
        first_thread = worker._thread
        worker.start()
        assert worker._thread is first_thread

        worker.stop()

    def test_refresh_golden_repo_type_also_works(self):
        """The refresh_golden_repo operation_type is also retryable."""
        claimer = FakeClaimer()
        claimer.jobs_to_return.append(
            {
                "job_id": "job-rgr",
                "operation_type": "refresh_golden_repo",
                "repo_alias": "other-repo-global",
            }
        )
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
        )

        worker._process_one_job()

        assert len(scheduler.refreshed_repos) == 1
        assert scheduler.refreshed_repos[0][0] == "other-repo-global"
        assert len(claimer.completed_jobs) == 1
        assert len(claimer.failed_jobs) == 0


class TestPollLoop:
    """Tests for the _poll_loop exception handling."""

    def test_poll_loop_catches_unexpected_exception(self):
        """Unexpected exception inside _process_one_job is caught by poll loop.

        The ExplodingClaimer raises inside claim_next_job(), which is called
        within _process_one_job(). This exception propagates up to the
        try/except in _poll_loop (lines 65-66), which catches it and logs it
        without killing the worker thread.
        """

        class ExplodingClaimer:
            def claim_next_job(self):
                raise RuntimeError("DB connection lost")

        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=ExplodingClaimer(),
            refresh_scheduler=scheduler,
            poll_interval=60,
        )

        # Start worker, let it run one iteration, then stop
        worker.start()
        # Give it a moment to run the loop once
        time.sleep(0.2)
        worker.stop()

        # Thread should have survived the exception and stopped cleanly
        assert not worker._thread.is_alive()


class TestExecuteRetryableJobDirect:
    """Tests for _execute_retryable_job called directly."""

    def test_execute_retryable_job_unknown_type_raises(self):
        """Direct call with unknown type raises ValueError."""
        claimer = FakeClaimer()
        scheduler = FakeRefreshScheduler()
        worker = DistributedJobWorkerService(
            claimer=claimer,
            refresh_scheduler=scheduler,
        )

        with pytest.raises(ValueError, match="Unknown retryable job type"):
            worker._execute_retryable_job("job-x", "weird_type", "repo")
