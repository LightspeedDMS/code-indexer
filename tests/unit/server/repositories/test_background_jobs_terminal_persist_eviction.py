"""
Regression tests for the terminal-persist-eviction concurrency bug.

ROOT CAUSE (confirmed): In _execute_job, on every terminal exit the job was
evicted from self.jobs unconditionally even if the terminal-status persist to
SQLite had silently failed (database locked / contention). This left the job
absent from both memory AND SQLite with a RUNNING status — causing
get_job_status() to permanently return "running".

FIX: _persist_jobs/_persist_single_job_sqlite/_persist_job_to_sqlite now
return bool. All three terminal eviction sites in _execute_job gate the
self.jobs.pop() on that bool. If persist fails, the job stays in memory with
its terminal status so callers see the correct state.

These tests inject a SQLite failure on exactly the terminal-status write and
assert correctness before and after the fix.
"""

import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig

# All tests are integration-style (real SQLite, real worker threads) but fast
pytestmark = pytest.mark.slow

_POLL_INTERVAL = 0.05  # seconds between status polls
_POLL_DEADLINE = 10.0  # seconds before declaring the test stuck
_EVICT_DEADLINE = 2.0  # seconds to wait for memory eviction on happy path


def _wait_for_terminal(
    manager: BackgroundJobManager,
    job_id: str,
    username: str = "testuser",
    deadline: float = _POLL_DEADLINE,
) -> Optional[Dict[str, Any]]:
    """Poll get_job_status until a terminal status appears or deadline expires.

    Returns the final status dict, or None if deadline expired (indicating the
    bug is present and the job appears stuck).
    """
    terminal_statuses = {"completed", "completed_partial", "failed", "cancelled"}
    deadline_at = time.monotonic() + deadline
    while time.monotonic() < deadline_at:
        status = manager.get_job_status(job_id, username=username, is_admin=True)
        if status and status.get("status") in terminal_statuses:
            return status
        time.sleep(_POLL_INTERVAL)
    # Return whatever get_job_status says at timeout (may still be "running")
    return manager.get_job_status(job_id, username=username, is_admin=True)


def _wait_for_not_in_running_jobs(
    manager: BackgroundJobManager,
    job_id: str,
    deadline: float = _POLL_DEADLINE,
) -> bool:
    """Wait until job_id is no longer in manager._running_jobs (worker finished)."""
    deadline_at = time.monotonic() + deadline
    while time.monotonic() < deadline_at:
        with manager._lock:
            if job_id not in manager._running_jobs:
                return True
        time.sleep(_POLL_INTERVAL)
    return False


def _wait_for_eviction(
    manager: BackgroundJobManager,
    job_id: str,
    deadline: float = _EVICT_DEADLINE,
) -> bool:
    """Wait until job_id is no longer in manager.jobs (memory evicted).

    Returns True if eviction happened within deadline, False if it did not.
    """
    deadline_at = time.monotonic() + deadline
    while time.monotonic() < deadline_at:
        with manager._lock:
            if job_id not in manager.jobs:
                return True
        time.sleep(_POLL_INTERVAL)
    return False


class TestTerminalPersistEviction:
    """Tests for the terminal-persist-eviction concurrency bug fix."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=5,
            ),
        )

    def teardown_method(self):
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _make_gated_job(self):
        """
        Return (job_func, proceed_event) where job_func blocks until
        proceed_event is set. This lets the test install its fault injection
        patch BEFORE the job body returns, guaranteeing the patch is in place
        when the terminal persist fires.
        """
        proceed = threading.Event()

        def gated_job():
            # Block until the test signals us to proceed
            proceed.wait(timeout=10.0)
            return {"success": True}

        return gated_job, proceed

    def _make_failing_gated_job(self):
        """Same as _make_gated_job but the job returns success=False."""
        proceed = threading.Event()

        def gated_failing_job():
            proceed.wait(timeout=10.0)
            return {"success": False, "error": "intentional failure"}

        return gated_failing_job, proceed

    def _inject_terminal_persist_failure_on_manager(self, target_job_id: str):
        """
        Patch _persist_single_job_sqlite on the manager instance so that the
        FIRST call for target_job_id returns False (simulating a silently-swallowed
        "database is locked" error — exactly what _persist_job_to_sqlite does when
        it catches sqlite3.OperationalError internally).

        IMPORTANT: We return False rather than raise. If we raised here, the
        exception would propagate through _persist_jobs (no try/except around the
        single-job path) and into _execute_job's broad except-Exception handler,
        which would incorrectly mark the job as FAILED and retry the persist.
        Returning False is the correct simulation of the actual failure mode.

        Safe against deadlock: we do NOT acquire self.manager._lock inside the
        patch. _persist_single_job_sqlite acquires the lock internally; acquiring
        it again in the wrapper would deadlock since the lock is not reentrant.

        The gated-job design guarantees correct injection timing:
        - The patch is installed only AFTER the job reaches RUNNING status.
        - The first call after that point is the terminal-status persist.
        - After firing once, subsequent calls fall through to the real method.

        Returns a tracker dict with 'fired' key.
        """
        original = self.manager._persist_single_job_sqlite
        tracker = {"fired": False}

        def patched(job_id: str) -> bool:
            if not tracker["fired"] and job_id == target_job_id:
                tracker["fired"] = True
                # Simulate a silently-swallowed SQLite failure (return False,
                # do NOT raise — see docstring for why raising is wrong here)
                return False
            return original(job_id)

        self.manager._persist_single_job_sqlite = patched  # type: ignore[method-assign]
        return tracker

    # ------------------------------------------------------------------
    # Bug-regression tests: persist failure must NOT produce a zombie
    # ------------------------------------------------------------------

    def test_terminal_persist_failure_job_stays_in_memory_as_completed(self):
        """
        RED before fix: get_job_status returns "running" forever.
        GREEN after fix: get_job_status returns "completed" because the job
        is retained in memory with its terminal status.

        Uses a gated job so the fault-injection patch is installed before
        the terminal persist fires, eliminating the setup race.
        """
        job_func, proceed = self._make_gated_job()

        job_id = self.manager.submit_job(
            "test_op", job_func, submitter_username="testuser"
        )

        # Wait until the job is RUNNING (worker picked it up and set status)
        deadline_at = time.monotonic() + _POLL_DEADLINE
        while time.monotonic() < deadline_at:
            with self.manager._lock:
                job = self.manager.jobs.get(job_id)
                if job and job.status == JobStatus.RUNNING:
                    break
            time.sleep(_POLL_INTERVAL)

        # Now install the patch — job is running but has not returned yet
        tracker = self._inject_terminal_persist_failure_on_manager(job_id)

        # Signal the job to complete; it will return {"success": True}
        proceed.set()

        # Wait for the worker thread to finish
        worker_finished = _wait_for_not_in_running_jobs(self.manager, job_id)
        assert worker_finished, "Worker thread did not finish within deadline"

        # Verify the fault injection actually fired
        assert tracker["fired"], (
            "Test setup error: terminal persist failure was never injected. "
            "The job may have completed before the patch was active."
        )

        # THE KEY ASSERTION: status must be terminal, not "running"
        status = self.manager.get_job_status(job_id, username="testuser", is_admin=True)
        assert status is not None, "get_job_status returned None — job not found at all"
        assert status["status"] == "completed", (
            f"Expected status='completed' after terminal persist failure, "
            f"got status='{status['status']}'. "
            "Bug present: job was evicted from memory despite failed persist."
        )

    def test_failed_job_persist_failure_stays_in_memory_as_failed(self):
        """
        When a job returns success=False (FAILED status) and the terminal
        persist fails, get_job_status must return 'failed', not 'running'.
        """
        job_func, proceed = self._make_failing_gated_job()

        job_id = self.manager.submit_job(
            "test_op", job_func, submitter_username="testuser"
        )

        # Wait until RUNNING
        deadline_at = time.monotonic() + _POLL_DEADLINE
        while time.monotonic() < deadline_at:
            with self.manager._lock:
                job = self.manager.jobs.get(job_id)
                if job and job.status == JobStatus.RUNNING:
                    break
            time.sleep(_POLL_INTERVAL)

        tracker = self._inject_terminal_persist_failure_on_manager(job_id)
        proceed.set()

        worker_finished = _wait_for_not_in_running_jobs(self.manager, job_id)
        assert worker_finished, "Worker thread did not finish within deadline"

        assert tracker["fired"], (
            "Test setup error: terminal persist failure was never injected."
        )

        status = self.manager.get_job_status(job_id, username="testuser", is_admin=True)
        assert status is not None, "get_job_status returned None — job not found"
        assert status["status"] == "failed", (
            f"Expected status='failed', got '{status['status']}'. "
            "Bug present: failed job evicted from memory despite failed persist."
        )

    # ------------------------------------------------------------------
    # Happy-path regression: normal eviction must still work
    # ------------------------------------------------------------------

    def test_terminal_persist_success_job_evicted_from_memory(self):
        """
        Happy path: when terminal persist SUCCEEDS, the job IS evicted from
        self.jobs (memory is bounded) and get_job_status still returns terminal
        (from SQLite fallback).
        """

        def quick_job():
            return {"success": True}

        job_id = self.manager.submit_job(
            "test_op", quick_job, submitter_username="testuser"
        )

        # Poll until we see a terminal status
        final_status = _wait_for_terminal(self.manager, job_id)
        assert final_status is not None, "Job did not reach terminal status in time"
        assert final_status["status"] == "completed", (
            f"Expected 'completed', got '{final_status['status']}'"
        )

        # Verify eviction happened (memory bounding)
        evicted = _wait_for_eviction(self.manager, job_id)
        assert evicted, (
            f"Job {job_id} was NOT evicted from self.manager.jobs after successful "
            "terminal persist. Memory bounding regression."
        )

        # get_job_status must still work (SQLite fallback)
        status_after_eviction = self.manager.get_job_status(
            job_id, username="testuser", is_admin=True
        )
        assert status_after_eviction is not None, (
            "get_job_status returned None after eviction — SQLite fallback broken"
        )
        assert status_after_eviction["status"] == "completed", (
            f"Expected 'completed' from SQLite fallback, "
            f"got '{status_after_eviction['status']}'"
        )

    def test_failed_job_terminal_persist_success_evicted(self):
        """
        Happy path for FAILED jobs: successful persist -> job evicted from
        memory -> still queryable via SQLite with status='failed'.
        """

        def failing_job():
            return {"success": False, "error": "intentional"}

        job_id = self.manager.submit_job(
            "test_op", failing_job, submitter_username="testuser"
        )

        final_status = _wait_for_terminal(self.manager, job_id)
        assert final_status is not None, "Job did not reach terminal status in time"
        assert final_status["status"] == "failed"

        evicted = _wait_for_eviction(self.manager, job_id)
        assert evicted, (
            "Failed job was NOT evicted from memory after successful terminal persist"
        )

        status_after = self.manager.get_job_status(
            job_id, username="testuser", is_admin=True
        )
        assert status_after is not None
        assert status_after["status"] == "failed"

    # ------------------------------------------------------------------
    # Verify _persist_jobs returns bool (the plumbing the fix relies on)
    # ------------------------------------------------------------------

    def test_persist_jobs_returns_true_on_success(self):
        """_persist_jobs must return True when the persist succeeds."""
        job_id = "plumbing-test-job"
        self.manager.jobs[job_id] = BackgroundJob(
            job_id=job_id,
            operation_type="test",
            status=JobStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            result={"success": True},
            error=None,
            progress=100,
            username="testuser",
        )
        result = self.manager._persist_jobs(job_id=job_id)
        assert result is True, (
            f"_persist_jobs should return True on success, got {result!r}"
        )

    def test_persist_jobs_returns_false_on_sqlite_error(self):
        """_persist_jobs must return False when the SQLite write fails."""
        job_id = "plumbing-fail-job"
        # Save the job first so the update_job path is taken on next call
        self.manager.jobs[job_id] = BackgroundJob(
            job_id=job_id,
            operation_type="test",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=50,
            username="testuser",
        )
        self.manager._persist_jobs(job_id=job_id)  # Write RUNNING to DB

        # Now make the update fail
        original_update = self.manager._sqlite_backend.update_job  # type: ignore[union-attr]

        def failing_update(job_id, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        self.manager._sqlite_backend.update_job = failing_update  # type: ignore[union-attr]
        try:
            # Update in-memory to terminal
            self.manager.jobs[job_id].status = JobStatus.COMPLETED
            self.manager.jobs[job_id].completed_at = datetime.now(timezone.utc)

            result = self.manager._persist_jobs(job_id=job_id)
            assert result is False, (
                f"_persist_jobs should return False on SQLite error, got {result!r}"
            )
        finally:
            self.manager._sqlite_backend.update_job = original_update  # type: ignore[union-attr]
