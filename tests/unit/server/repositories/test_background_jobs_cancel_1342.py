"""
Unit tests for BackgroundJobManager cancellation wiring (Bug #1342).

Cancelling a running activation job used to be a no-op while the worker was
blocked inside a long subprocess: cancel only set job.cancelled=True, and
nobody ever checked it during the subprocess call. Separately, JobTracker was
never told about a cancellation, so its `_active_jobs` entry (last set to
"running") survived forever as a dashboard "zombie" -- shown as running in
recent-activity even after the job's own DB row read CANCELLED.

These tests prove, end-to-end against a REAL BackgroundJobManager (SQLite
backend) and a REAL JobTracker (SQLite, no mocks):

1. `cancel_check` is signature-injected into the worker function exactly
   like `progress_callback` already is, and — combined with the real
   `run_cancellable_subprocess` engine — kills a REAL bash subprocess
   promptly when the job is cancelled (this is the activation shape:
   progress_callback + cancel_check both present).
2. All three cancel-finalization exit paths in `_execute_job`
   (success-after-cancel, InterruptedError, exception-after-cancel) now
   call `JobTracker.cancel_job`, so the tracker's `_active_jobs` entry is
   actually removed -- not a permanent "running" zombie.

   Note: these tests verify `JobTracker.cancel_job` is invoked (via a
   call-recording spy around the real implementation) and that the job is
   removed from `_active_jobs`, rather than polling the SQLite `status`
   column. Investigating this suite's flakiness surfaced a PRE-EXISTING,
   unrelated race in `BackgroundJobManager.cancel_job()`'s own persist call
   for a still-running job: it persists a stale snapshot (status="running",
   cancelled=True) on the caller's thread, and SQLite's BEGIN EXCLUSIVE lock
   contention can occasionally delay that commit until AFTER the worker
   thread's own later, correct terminal write, transiently reverting the
   `status` column. That race predates Bug #1342 and is out of scope here;
   the `_active_jobs` removal (the actual "zombie" fix) is unaffected by it.

Mocking policy: NO process/subprocess mocks (test 1 spawns a real `bash`
child). BackgroundJobManager and JobTracker are REAL instances against a
real temp SQLite DB, mirroring test_background_job_manager_tracker.py's
existing `bgm`/`tracker`/`db_path` fixture conventions.
"""

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.utils.config_manager import BackgroundJobsConfig

pytestmark = pytest.mark.slow

# Test-only timing constants (not production defaults).
_PRE_CANCEL_SLEEP_SECONDS = 0.05
_WORKER_SLEEP_SECONDS = 0.2
_LONG_WORKER_SLEEP_SECONDS = 5.0
_POLL_TIMEOUT_SECONDS = 3.0
_POLL_INTERVAL_SECONDS = 0.05


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(temp_dir):
    path = str(Path(temp_dir) / "test_bgm_cancel_1342.db")
    DatabaseSchema(path).initialize_database()
    return path


@pytest.fixture
def tracker(db_path):
    return JobTracker(db_path)


@pytest.fixture
def bgm(db_path, tracker):
    manager = BackgroundJobManager(
        use_sqlite=True,
        db_path=db_path,
        background_jobs_config=BackgroundJobsConfig(max_concurrent_background_jobs=10),
        job_tracker=tracker,
    )
    yield manager
    manager.shutdown()


def _poll_until(
    predicate: Callable[[], bool],
    timeout: float,
    interval: float = _POLL_INTERVAL_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestCancelCheckInjectionKillsRealSubprocess:
    def test_cancel_check_injected_kills_real_subprocess_promptly(self, bgm):
        """Real end-to-end proof: a worker shaped like _do_activate_repository
        (accepts both progress_callback and cancel_check) gets a working
        cancel_check injected by _execute_job, and a real subprocess it
        drives via run_cancellable_subprocess dies promptly on cancel."""
        from code_indexer.server.utils.cancellable_subprocess import (
            SubprocessCancelledError,
            run_cancellable_subprocess,
        )

        started = threading.Event()
        outcome: dict = {"cancelled": False}

        def activation_shaped_worker(progress_callback=None, cancel_check=None):
            if progress_callback:
                progress_callback(10)
            started.set()
            try:
                run_cancellable_subprocess(
                    ["bash", "-c", "sleep 30"],
                    cwd="/tmp",
                    cancel_check=cancel_check,
                    poll_interval=0.05,
                )
            except SubprocessCancelledError:
                outcome["cancelled"] = True
            return {"success": True}

        job_id = bgm.submit_job(
            "activate_repository",
            activation_shaped_worker,
            submitter_username="admin",
        )

        assert started.wait(timeout=2.0), "worker did not start in time"

        start = time.monotonic()
        result = bgm.cancel_job(job_id, username="admin")
        assert result["success"] is True

        reached_terminal = _poll_until(
            lambda: bgm.get_job_status(job_id, username="admin")["status"]
            in ("cancelled", "failed", "completed"),
            timeout=5.0,
        )
        elapsed = time.monotonic() - start
        assert reached_terminal, "job never reached a terminal status"
        assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s, expected < 5s"
        assert outcome["cancelled"] is True, (
            "run_cancellable_subprocess must have been cancelled, proving "
            "cancel_check was actually injected and honored"
        )


class TestTrackerCancelJobCalledFromAllExitPaths:
    """JobTracker.cancel_job must be called from every cancel-finalization
    exit path so no cancelled job survives as a running zombie."""

    @staticmethod
    def _spy_on_cancel_job(tracker):
        """Wrap the REAL tracker.cancel_job with a call-recording spy.

        The real implementation still runs (this is not a mock of behavior
        under test) -- only call arguments/count are additionally recorded,
        so tests can assert cancel_job was actually invoked without relying
        on a subsequent DB read (which is subject to the pre-existing,
        unrelated commit-ordering race documented in this module's
        docstring).
        """
        calls: list = []
        original = tracker.cancel_job

        def spy(job_id):
            calls.append(job_id)
            return original(job_id)

        tracker.cancel_job = spy
        return calls

    def test_success_after_cancel_calls_tracker_cancel_job(self, bgm, tracker):
        """Worker (progress_callback-shaped) completes successfully despite
        being marked cancelled mid-flight -- the 'success-after-cancel'
        split-brain path."""
        calls = self._spy_on_cancel_job(tracker)

        def slow_success(progress_callback=None):
            if progress_callback:
                progress_callback(10)
            time.sleep(_WORKER_SLEEP_SECONDS)
            return {"success": True}

        job_id = bgm.submit_job(
            "slow_success_op",
            slow_success,
            submitter_username="admin",
        )
        time.sleep(_PRE_CANCEL_SLEEP_SECONDS)
        result = bgm.cancel_job(job_id, username="admin")
        assert result["success"] is True

        assert _poll_until(lambda: job_id in calls, timeout=_POLL_TIMEOUT_SECONDS)

        assert _poll_until(
            lambda: job_id not in _active_job_ids(tracker),
            timeout=_POLL_TIMEOUT_SECONDS,
        ), "cancelled job must be removed from _active_jobs"

    def test_interrupted_error_path_calls_tracker_cancel_job(self, bgm, tracker):
        """Worker WITHOUT progress_callback: goes through
        _execute_with_cancellation_check's InterruptedError path."""
        calls = self._spy_on_cancel_job(tracker)

        def slow_no_progress_callback():
            time.sleep(_LONG_WORKER_SLEEP_SECONDS)
            return {"success": True}

        job_id = bgm.submit_job(
            "slow_no_callback_op",
            slow_no_progress_callback,
            submitter_username="admin",
        )
        time.sleep(_PRE_CANCEL_SLEEP_SECONDS)
        result = bgm.cancel_job(job_id, username="admin")
        assert result["success"] is True

        assert _poll_until(lambda: job_id in calls, timeout=_POLL_TIMEOUT_SECONDS)

        assert _poll_until(
            lambda: job_id not in _active_job_ids(tracker),
            timeout=_POLL_TIMEOUT_SECONDS,
        ), "cancelled job must be removed from _active_jobs"

    def test_exception_after_cancel_calls_tracker_cancel_job_not_fail_job(
        self, bgm, tracker
    ):
        """Worker (progress_callback-shaped) raises an exception after being
        marked cancelled -- must finalize via cancel_job, never fail_job."""
        cancel_calls = self._spy_on_cancel_job(tracker)
        fail_calls: list = []
        original_fail_job = tracker.fail_job

        def fail_spy(job_id, error):
            fail_calls.append(job_id)
            return original_fail_job(job_id, error)

        tracker.fail_job = fail_spy

        def slow_then_raise(progress_callback=None):
            if progress_callback:
                progress_callback(10)
            time.sleep(_WORKER_SLEEP_SECONDS)
            raise RuntimeError("boom")

        job_id = bgm.submit_job(
            "slow_then_raise_op",
            slow_then_raise,
            submitter_username="admin",
        )
        time.sleep(_PRE_CANCEL_SLEEP_SECONDS)
        result = bgm.cancel_job(job_id, username="admin")
        assert result["success"] is True

        assert _poll_until(
            lambda: job_id in cancel_calls, timeout=_POLL_TIMEOUT_SECONDS
        )
        assert job_id not in fail_calls, (
            "a cancelled job must never be finalized via fail_job"
        )

        assert _poll_until(
            lambda: job_id not in _active_job_ids(tracker),
            timeout=_POLL_TIMEOUT_SECONDS,
        ), "cancelled job must be removed from _active_jobs"


def _active_job_ids(tracker) -> set:
    with tracker._lock:
        return set(tracker._active_jobs.keys())
