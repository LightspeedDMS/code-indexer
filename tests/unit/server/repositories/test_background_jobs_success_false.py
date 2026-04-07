"""
Unit tests for Bug #646: BackgroundJobManager must mark jobs FAILED when
job function returns {"success": False, ...}, not COMPLETED.

TDD Red phase: these tests are written BEFORE the fix to confirm the bug exists.
After the fix they serve as the regression suite.

Covered scenarios:
1. result={"success": False, "error": "..."} → JobStatus.FAILED
2. result={"success": True} → JobStatus.COMPLETED (happy path preserved)
3. result=None → JobStatus.COMPLETED (non-dict return preserved)
4. tracker notified with fail_job() when result["success"] is False
5. tracker notified with complete_job() when result["success"] is True
6. result dict persisted on job even when success=False
7. result["success"]=0 (non-bool falsy) treated as COMPLETED (only `is False` triggers FAILED)
8. FAILED job removed from in-memory dict when SQLite backend is active (memory leak fix)
"""

import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig

# Named constants — no magic numbers inline
_MAX_CONCURRENT_JOBS = 10
_DEFAULT_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05
_TEST_USERNAME = "test_user"


class TestSuccessFalseMarkedFailed:
    """Tests for Bug #646: result["success"]=False must produce FAILED status."""

    def setup_method(self):
        """Create an in-memory manager (no SQLite) so jobs stay in self.jobs."""
        self.manager = BackgroundJobManager(
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
            ),
        )

    def teardown_method(self):
        self.manager.shutdown()

    # ------------------------------------------------------------------
    # Helper: submit a job and wait for it to finish, returning the job.
    # ------------------------------------------------------------------

    def _run_job_and_wait(self, func, timeout: float = _DEFAULT_TIMEOUT_SECONDS):
        """Submit func as a background job, wait for completion, return job.

        Access jobs directly from self.manager.jobs because no SQLite backend
        is configured, so completed/failed jobs remain in memory.
        """
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

    # ------------------------------------------------------------------
    # Test 1 — Regression: success=False must produce FAILED
    # ------------------------------------------------------------------

    def test_job_returning_success_false_is_marked_failed(self):
        """Bug #646: job returning {"success": False} must have status FAILED."""

        def failing_job():
            return {"success": False, "error": "Something went wrong"}

        job = self._run_job_and_wait(failing_job)
        assert job is not None, "Job was not found after completion"
        assert job.status == JobStatus.FAILED, (
            f"Expected FAILED but got {job.status}. "
            "Bug #646: result['success']=False must set status to FAILED."
        )

    # ------------------------------------------------------------------
    # Test 2 — Happy path: success=True must still produce COMPLETED
    # ------------------------------------------------------------------

    def test_job_returning_success_true_is_marked_completed(self):
        """Happy path preserved: result["success"]=True → COMPLETED."""

        def good_job():
            return {"success": True, "data": "ok"}

        job = self._run_job_and_wait(good_job)
        assert job is not None, "Job was not found after completion"
        assert job.status == JobStatus.COMPLETED, (
            f"Expected COMPLETED but got {job.status}."
        )

    # ------------------------------------------------------------------
    # Test 3 — Non-dict return: None must still produce COMPLETED
    # ------------------------------------------------------------------

    def test_job_returning_none_is_marked_completed(self):
        """Non-dict return preserved: result=None → COMPLETED."""

        def none_job():
            return None

        job = self._run_job_and_wait(none_job)
        assert job is not None, "Job was not found after completion"
        assert job.status == JobStatus.COMPLETED, (
            f"Expected COMPLETED but got {job.status}."
        )

    # ------------------------------------------------------------------
    # Test 4 — Tracker notified: fail_job() called (not complete_job())
    # ------------------------------------------------------------------

    def test_tracker_fail_job_called_when_success_false(self):
        """When result["success"]=False, tracker.fail_job() is called, not complete_job()."""
        mock_tracker = MagicMock()
        self.manager._job_tracker = mock_tracker

        def failing_job():
            return {"success": False, "error": "index build failed"}

        job = self._run_job_and_wait(failing_job)
        assert job is not None, "Job was not found after completion"

        mock_tracker.fail_job.assert_called_once()
        mock_tracker.complete_job.assert_not_called()

    # ------------------------------------------------------------------
    # Test 5 — Tracker: complete_job() still called for success=True
    # ------------------------------------------------------------------

    def test_tracker_complete_job_called_when_success_true(self):
        """When result["success"]=True, tracker.complete_job() is called, not fail_job()."""
        mock_tracker = MagicMock()
        self.manager._job_tracker = mock_tracker

        def good_job():
            return {"success": True}

        job = self._run_job_and_wait(good_job)
        assert job is not None, "Job was not found after completion"

        mock_tracker.complete_job.assert_called_once()
        mock_tracker.fail_job.assert_not_called()

    # ------------------------------------------------------------------
    # Test 6 — result dict preserved: job.result still holds the dict
    # ------------------------------------------------------------------

    def test_job_result_stored_when_success_false(self):
        """The result dict is stored on the job even when success=False."""

        def failing_job():
            return {"success": False, "error": "disk full"}

        job = self._run_job_and_wait(failing_job)
        assert job is not None
        assert job.result == {"success": False, "error": "disk full"}, (
            f"Expected result dict preserved, got {job.result}"
        )

    # ------------------------------------------------------------------
    # Test 7 — Non-bool falsy value: success=0 is NOT `is False`, so COMPLETED
    # ------------------------------------------------------------------

    def test_job_returning_success_zero_is_marked_completed(self):
        """Boundary: result["success"]=0 is not `is False`, so status is COMPLETED.

        The fix uses `result.get("success") is False` (identity check), meaning only
        the exact Python singleton False triggers FAILED. The integer 0 does not match
        and therefore results in COMPLETED.
        """

        def zero_job():
            return {"success": 0}

        job = self._run_job_and_wait(zero_job)
        assert job is not None
        assert job.status == JobStatus.COMPLETED, (
            f"Expected COMPLETED for success=0 (not `is False`), got {job.status}"
        )


class TestFailedJobMemoryCleanup:
    """Tests for memory-leak fix: FAILED jobs must be evicted from in-memory dict
    when SQLite backend is active (same as COMPLETED and CANCELLED jobs)."""

    _MAX_CONCURRENT_JOBS = 10
    _DEFAULT_TIMEOUT_SECONDS = 5.0
    _POLL_INTERVAL_SECONDS = 0.05
    _TEST_USERNAME = "test_user"

    def setup_method(self):
        """Create a manager backed by a real SQLite database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=self._MAX_CONCURRENT_JOBS,
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
        """Submit job and wait until it is evicted from self.manager.jobs.

        Eviction happens in the background thread after persist completes, which
        is after the terminal status is written. Polling only for terminal status
        would exit before eviction runs, causing a race. This helper polls until
        the job is absent from the in-memory dict — the definitive eviction signal.
        Returns job_id so callers can assert on it.
        """
        job_id = self.manager.submit_job(
            operation_type="test_op",
            func=func,
            submitter_username=self._TEST_USERNAME,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if job_id not in self.manager.jobs:
                return job_id
            time.sleep(self._POLL_INTERVAL_SECONDS)
        # Timeout expired — return job_id so the caller can assert and produce
        # a meaningful failure message.
        return job_id

    def test_failed_job_removed_from_memory_when_sqlite_backend_active(self):
        """Memory-leak fix: FAILED job must be evicted from self.jobs when SQLite active.

        Before the fix, only COMPLETED and CANCELLED were removed; FAILED jobs leaked.
        After the fix, FAILED is included in the eviction tuple.
        """

        def failing_job():
            return {"success": False, "error": "simulated failure"}

        job_id = self._submit_and_wait_for_eviction(failing_job)

        # The job must NOT be in memory (evicted to SQLite)
        assert job_id not in self.manager.jobs, (
            f"FAILED job {job_id} was NOT evicted from in-memory dict. "
            "Memory leak: FAILED jobs must be removed when SQLite backend is active."
        )

    def test_completed_job_still_removed_from_memory_when_sqlite_backend_active(self):
        """Regression guard: COMPLETED jobs must still be evicted (existing behaviour)."""

        def good_job():
            return {"success": True}

        job_id = self._submit_and_wait_for_eviction(good_job)

        assert job_id not in self.manager.jobs, (
            f"COMPLETED job {job_id} was NOT evicted from in-memory dict. "
            "Regression: COMPLETED eviction must still work."
        )
