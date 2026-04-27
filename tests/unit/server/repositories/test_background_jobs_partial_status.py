"""
Unit tests for Bug #679 AC1: COMPLETED_PARTIAL JobStatus enum.

Covers:
- test_completed_partial_is_valid_status
- test_completed_partial_stored_and_retrieved
- test_provider_results_stored_in_job_result_dict
- test_completed_partial_removed_from_memory_when_sqlite_active
- test_job_tracker_notified_on_completed_partial
- test_exit_code_mapping: 0->COMPLETED, 1->FAILED, 2->COMPLETED_PARTIAL

All tests drive the real BackgroundJobManager SUT and assert on actual job state.
"""

import os
import shutil
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig

# Named constants — no magic numbers inline
_MAX_CONCURRENT_JOBS = 10
_DEFAULT_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05
_TEST_USERNAME = "test_user"

_PROVIDER_RESULTS_FULL_SUCCESS = {
    "voyage-ai": {
        "status": "success",
        "error": None,
        "latency_seconds": 142.3,
        "files_indexed": 4821,
        "chunks_indexed": 12451,
    },
    "cohere": {
        "status": "success",
        "error": None,
        "latency_seconds": 90.0,
        "files_indexed": 4821,
        "chunks_indexed": 12451,
    },
}

_PROVIDER_RESULTS_PARTIAL = {
    "voyage-ai": {
        "status": "success",
        "error": None,
        "latency_seconds": 142.3,
        "files_indexed": 4821,
        "chunks_indexed": 12451,
    },
    "cohere": {
        "status": "failed",
        "error": "TimeoutError after 30s",
        "latency_seconds": 30.1,
        "files_indexed": 0,
        "chunks_indexed": 0,
    },
}


class TestCompletedPartialEnum:
    """Tests for COMPLETED_PARTIAL enum value existence and validity."""

    def test_completed_partial_is_valid_status(self):
        """AC1: COMPLETED_PARTIAL must exist in JobStatus enum."""
        assert hasattr(JobStatus, "COMPLETED_PARTIAL"), (
            "JobStatus must have a COMPLETED_PARTIAL member. "
            "Bug #679 AC1: add COMPLETED_PARTIAL = 'completed_partial'."
        )
        assert JobStatus.COMPLETED_PARTIAL.value == "completed_partial", (
            f"Expected value 'completed_partial', got {JobStatus.COMPLETED_PARTIAL.value!r}"
        )

    def test_completed_partial_distinct_from_completed_and_failed(self):
        """COMPLETED_PARTIAL must be different from COMPLETED and FAILED."""
        assert JobStatus.COMPLETED_PARTIAL != JobStatus.COMPLETED
        assert JobStatus.COMPLETED_PARTIAL != JobStatus.FAILED


class TestCompletedPartialInMemoryManager:
    """Tests for COMPLETED_PARTIAL through BackgroundJobManager (in-memory, no SQLite).

    The contract: a job function returning {"partial": True, ...} (with success not
    False) causes BackgroundJobManager to set job.status = COMPLETED_PARTIAL.
    """

    def setup_method(self):
        """Create an in-memory manager (no SQLite) so jobs stay in self.jobs."""
        self.manager = BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
            ),
        )

    def teardown_method(self):
        self.manager.shutdown()

    def _run_job_and_wait(self, func, timeout: float = _DEFAULT_TIMEOUT_SECONDS):
        """Submit func as a background job, wait for terminal status, return job."""
        job_id = self.manager.submit_job(
            operation_type="test_op",
            func=func,
            submitter_username=_TEST_USERNAME,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.manager.jobs.get(job_id)
            if job and job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                return job
            time.sleep(_POLL_INTERVAL_SECONDS)
        return self.manager.jobs.get(job_id)

    def test_completed_partial_stored_and_retrieved(self):
        """A job returning partial=True is stored with COMPLETED_PARTIAL status."""

        def partial_success_job():
            return {
                "partial": True,
                "provider_results": _PROVIDER_RESULTS_PARTIAL,
            }

        job = self._run_job_and_wait(partial_success_job)
        assert job is not None, "Job was not found after completion"
        assert job.status == JobStatus.COMPLETED_PARTIAL, (
            f"Expected COMPLETED_PARTIAL but got {job.status}. "
            "Bug #679 AC1: result['partial']=True must set status to COMPLETED_PARTIAL."
        )

    def test_provider_results_stored_in_job_result_dict(self):
        """Provider results dict is preserved in job.result for partial jobs."""

        def job_with_partial_results():
            return {
                "partial": True,
                "provider_results": _PROVIDER_RESULTS_PARTIAL,
            }

        job = self._run_job_and_wait(job_with_partial_results)
        assert job is not None
        assert isinstance(job.result, dict)
        assert job.result["provider_results"] == _PROVIDER_RESULTS_PARTIAL

    def test_full_success_job_is_still_completed(self):
        """Regression guard: job without partial=True remains COMPLETED."""

        def full_success_job():
            return {
                "provider_results": _PROVIDER_RESULTS_FULL_SUCCESS,
            }

        job = self._run_job_and_wait(full_success_job)
        assert job is not None
        assert job.status == JobStatus.COMPLETED, (
            f"Expected COMPLETED for full success, got {job.status}"
        )

    def test_success_false_still_produces_failed(self):
        """Regression guard: result['success']=False still produces FAILED."""

        def failing_job():
            return {"success": False, "error": "all providers failed"}

        job = self._run_job_and_wait(failing_job)
        assert job is not None
        assert job.status == JobStatus.FAILED, (
            f"Expected FAILED for success=False, got {job.status}"
        )

    def test_job_tracker_notified_on_completed_partial(self):
        """AC1 line 766 audit: tracker.complete_job() called for COMPLETED_PARTIAL.

        COMPLETED_PARTIAL is treated as a completion variant (not failure),
        so complete_job() must be invoked, not fail_job().
        """
        mock_tracker = MagicMock()
        self.manager._job_tracker = mock_tracker

        def partial_success_job():
            return {
                "partial": True,
                "provider_results": _PROVIDER_RESULTS_PARTIAL,
            }

        job = self._run_job_and_wait(partial_success_job)
        assert job is not None
        assert job.status == JobStatus.COMPLETED_PARTIAL

        mock_tracker.complete_job.assert_called_once()
        mock_tracker.fail_job.assert_not_called()

    def test_tracker_fail_job_not_called_for_completed_partial(self):
        """COMPLETED_PARTIAL must not trigger fail_job() in the tracker."""
        mock_tracker = MagicMock()
        self.manager._job_tracker = mock_tracker

        def partial_success_job():
            return {"partial": True, "provider_results": _PROVIDER_RESULTS_PARTIAL}

        job = self._run_job_and_wait(partial_success_job)
        assert job is not None
        assert job.status == JobStatus.COMPLETED_PARTIAL
        mock_tracker.fail_job.assert_not_called()

    def test_partial_false_does_not_trigger_completed_partial(self):
        """Boundary: result['partial']=False must not produce COMPLETED_PARTIAL."""

        def not_partial_job():
            return {
                "partial": False,
                "provider_results": _PROVIDER_RESULTS_FULL_SUCCESS,
            }

        job = self._run_job_and_wait(not_partial_job)
        assert job is not None
        assert job.status == JobStatus.COMPLETED, (
            f"Expected COMPLETED for partial=False, got {job.status}"
        )

    def test_partial_zero_does_not_trigger_completed_partial(self):
        """Boundary: result['partial']=0 (falsy non-bool) must not trigger COMPLETED_PARTIAL.

        The check uses `is True` identity, so only the exact Python singleton True triggers it.
        """

        def zero_partial_job():
            return {"partial": 0}

        job = self._run_job_and_wait(zero_partial_job)
        assert job is not None
        assert job.status == JobStatus.COMPLETED, (
            f"Expected COMPLETED for partial=0 (not `is True`), got {job.status}"
        )


class TestCompletedPartialWithSQLite:
    """Tests for COMPLETED_PARTIAL with SQLite backend.

    Verifies memory eviction, persistence, and cleanup_old_jobs behavior.
    """

    def setup_method(self):
        """Create a manager backed by a real SQLite database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
            ),
        )

    def teardown_method(self):
        """Clean up temp dir after each test."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _submit_and_wait_for_eviction(
        self, func, timeout: float = _DEFAULT_TIMEOUT_SECONDS
    ) -> str:
        """Submit job and wait until evicted from self.manager.jobs (persisted to SQLite)."""
        job_id = self.manager.submit_job(
            operation_type="test_op",
            func=func,
            submitter_username=_TEST_USERNAME,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job_id not in self.manager.jobs:
                return str(job_id)
            time.sleep(_POLL_INTERVAL_SECONDS)
        return str(job_id)

    def test_completed_partial_removed_from_memory_when_sqlite_active(self):
        """AC1 lines 791-794 audit: COMPLETED_PARTIAL must be evicted like COMPLETED.

        Before the fix, only COMPLETED/CANCELLED/FAILED were in the eviction tuple.
        After Bug #679, COMPLETED_PARTIAL must also be evicted when SQLite is active.
        """

        def partial_success_job():
            return {"partial": True, "provider_results": _PROVIDER_RESULTS_PARTIAL}

        job_id = self._submit_and_wait_for_eviction(partial_success_job)

        assert job_id not in self.manager.jobs, (
            f"COMPLETED_PARTIAL job {job_id} was NOT evicted from in-memory dict. "
            "Bug #679 AC1: COMPLETED_PARTIAL must be in the eviction set."
        )

    def test_completed_partial_persisted_to_sqlite(self):
        """COMPLETED_PARTIAL job is readable from SQLite after eviction."""

        def partial_success_job():
            return {"partial": True, "provider_results": _PROVIDER_RESULTS_PARTIAL}

        job_id = self._submit_and_wait_for_eviction(partial_success_job)

        # After eviction, fetch from SQLite
        assert self.manager._sqlite_backend is not None
        db_job = self.manager._sqlite_backend.get_job(job_id)
        assert db_job is not None, f"Job {job_id} not found in SQLite after eviction"
        assert db_job.get("status") == "completed_partial", (
            f"Expected 'completed_partial' in SQLite, got {db_job.get('status')!r}"
        )

    def test_cleanup_old_jobs_removes_completed_partial(self):
        """AC1 line 936 audit: cleanup_old_jobs must include COMPLETED_PARTIAL.

        We inject a COMPLETED_PARTIAL job with old completed_at directly into the
        manager's in-memory dict, then call cleanup_old_jobs() and verify removal.
        """
        old_time = datetime.now(timezone.utc) - timedelta(hours=48)
        fake_job = BackgroundJob(
            job_id="fake-partial-job-id",
            operation_type="test_op",
            status=JobStatus.COMPLETED_PARTIAL,
            created_at=old_time,
            started_at=old_time,
            completed_at=old_time,
            result={"partial": True},
            error=None,
            progress=100,
            username=_TEST_USERNAME,
        )

        with self.manager._lock:
            self.manager.jobs["fake-partial-job-id"] = fake_job

        # Now run cleanup with 24h threshold — the 48h-old job must be removed
        cleaned = self.manager.cleanup_old_jobs(max_age_hours=24)

        assert "fake-partial-job-id" not in self.manager.jobs, (
            "COMPLETED_PARTIAL job with old completed_at was NOT cleaned up. "
            "Bug #679 AC1: cleanup_old_jobs must include COMPLETED_PARTIAL in its status set."
        )
        assert cleaned >= 1, (
            f"Expected cleanup_old_jobs to report >=1 cleaned, got {cleaned}"
        )
