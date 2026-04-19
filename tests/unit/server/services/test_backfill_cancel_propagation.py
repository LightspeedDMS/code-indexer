"""
Unit tests for Bug #853 Fix 5: DescriptionRefreshScheduler must provide
set_active_backfill_job_id() and stop processing repos when the backfill job
is cancelled, calling fail_job with the correct job_id.

TDD Red phase: tests written BEFORE the fix.

Covered scenarios:
1. DescriptionRefreshScheduler has set_active_backfill_job_id() method
2. _active_backfill_job_id initialised to None in __init__
3. set_active_backfill_job_id stores the job_id on the instance
4. set_active_backfill_job_id(None) clears the stored id
5. When backfill job is cancelled:
   - fail_job called with the exact job_id
   - claude_cli_manager.submit_work NOT called (processing stopped)
6. When backfill job is still running:
   - fail_job NOT called
   - claude_cli_manager IS invoked for stale repos (normal processing proceeds)
7. When no active backfill job_id is set:
   - get_job and fail_job NOT called (cancellation check skipped)
"""

import os
import shutil
import sqlite3
import tempfile
from unittest.mock import MagicMock, Mock

from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.services.job_tracker import JobTracker, TrackedJob

_BACKFILL_JOB_ID = "lifecycle-backfill-test1234"
_REPO_ALIAS = "repo-a"
_REPO_PATH = "/fake/path/repo-a"


def _make_tracking_backend(stale_repos: list = None):
    """Build a fake DescriptionRefreshTrackingBackend."""
    backend = Mock()
    backend.get_stale_repos.return_value = stale_repos or []
    backend.upsert_tracking.return_value = None
    return backend


def _make_golden_backend(repos: dict = None):
    """Build a fake GoldenRepoMetadataSqliteBackend."""
    backend = Mock()
    if repos:
        backend.get_repo.side_effect = lambda alias: repos.get(alias)
    else:
        backend.get_repo.return_value = None
    return backend


def _make_stale_repo_record(alias: str = _REPO_ALIAS, path: str = _REPO_PATH) -> dict:
    """Build a stale repo record as returned by get_stale_repos."""
    return {
        "repo_alias": alias,
        "clone_path": path,
        "lifecycle_schema_version": None,
    }


def _make_scheduler(
    job_tracker=None,
    stale_repos=None,
    golden_repos=None,
    claude_cli_manager=None,
):
    """Build a DescriptionRefreshScheduler with injectable backends (no db_path required)."""
    tracking_backend = _make_tracking_backend(stale_repos)
    golden_backend = _make_golden_backend(golden_repos)

    config_manager = Mock()
    config_manager.load_config.return_value = None

    return DescriptionRefreshScheduler(
        config_manager=config_manager,
        claude_cli_manager=claude_cli_manager,
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        job_tracker=job_tracker,
    )


def _make_tracked_job(job_id: str, status: str) -> TrackedJob:
    """Build a TrackedJob with the given status."""
    return TrackedJob(
        job_id=job_id,
        operation_type="lifecycle_backfill",
        status=status,
        username="system",
    )


class TestSchedulerHasSetActiveBackfillJobId:
    """Fix 5: DescriptionRefreshScheduler must expose set_active_backfill_job_id."""

    def test_has_set_active_backfill_job_id_method(self):
        """DescriptionRefreshScheduler must have a set_active_backfill_job_id method."""
        scheduler = _make_scheduler()

        has_method = hasattr(scheduler, "set_active_backfill_job_id") and callable(
            getattr(scheduler, "set_active_backfill_job_id")
        )

        assert has_method is True, (
            "DescriptionRefreshScheduler must have set_active_backfill_job_id() method"
        )

    def test_active_backfill_job_id_starts_as_none(self):
        """_active_backfill_job_id is None when no job has been set."""
        scheduler = _make_scheduler()

        has_attr = hasattr(scheduler, "_active_backfill_job_id")
        assert has_attr is True, (
            "DescriptionRefreshScheduler must have _active_backfill_job_id attribute"
        )
        assert scheduler._active_backfill_job_id is None

    def test_set_active_backfill_job_id_stores_value(self):
        """set_active_backfill_job_id stores the given job_id on the instance."""
        scheduler = _make_scheduler()

        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        stored = scheduler._active_backfill_job_id
        assert stored == _BACKFILL_JOB_ID, (
            f"Expected {_BACKFILL_JOB_ID!r} stored, got {stored!r}"
        )

    def test_set_active_backfill_job_id_accepts_none_to_clear(self):
        """set_active_backfill_job_id(None) clears the stored job_id."""
        scheduler = _make_scheduler()
        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        scheduler.set_active_backfill_job_id(None)

        stored = scheduler._active_backfill_job_id
        assert stored is None, f"Expected None after clearing, got {stored!r}"


class TestSchedulerCancellationPropagation:
    """Fix 5: _run_loop_single_pass must stop repo processing and call fail_job
    when the active backfill job is cancelled, and proceed normally when running."""

    def test_cancelled_backfill_calls_update_status_cancelled_and_skips_repo_processing(
        self,
    ):
        """
        When backfill is cancelled: update_status called with (_BACKFILL_JOB_ID,
        status='cancelled') AND no repo processing. fail_job must NOT be called.
        """
        mock_tracker = MagicMock()
        mock_tracker.is_cancelled.return_value = True

        mock_cli_manager = Mock()
        stale_repos = [_make_stale_repo_record()]
        golden_repos = {_REPO_ALIAS: {"clone_path": _REPO_PATH}}

        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=stale_repos,
            golden_repos=golden_repos,
            claude_cli_manager=mock_cli_manager,
        )
        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        scheduler._run_loop_single_pass()

        # fail_job must NOT be called — cancellation is not a failure
        assert mock_tracker.fail_job.call_count == 0, (
            f"fail_job must NOT be called for cancellation, "
            f"was called {mock_tracker.fail_job.call_count} times"
        )

        # update_status must be called with both _BACKFILL_JOB_ID and status='cancelled'
        all_calls = mock_tracker.update_status.call_args_list
        matching_calls = [
            c
            for c in all_calls
            if (
                c[0]
                and c[0][0] == _BACKFILL_JOB_ID
                and c[1].get("status") == "cancelled"
            )
            or (
                len(c[0]) > 1 and c[0][0] == _BACKFILL_JOB_ID and c[0][1] == "cancelled"
            )
        ]
        assert len(matching_calls) >= 1, (
            f"update_status must be called with ({_BACKFILL_JOB_ID!r}, status='cancelled'). "
            f"Actual calls: {all_calls}"
        )

        # Processing must stop
        assert mock_cli_manager.submit_work.call_count == 0, (
            "submit_work must not be called when backfill is cancelled"
        )

    def test_running_backfill_does_not_call_fail_job_and_processes_repos(self):
        """
        When backfill is NOT cancelled: is_cancelled called (replacing get_job),
        fail_job NOT called, loop continues normally.
        """
        mock_tracker = MagicMock()
        mock_tracker.is_cancelled.return_value = False

        stale_repos = [_make_stale_repo_record()]
        golden_repos = {_REPO_ALIAS: {"clone_path": _REPO_PATH}}

        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=stale_repos,
            golden_repos=golden_repos,
        )
        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        scheduler._run_loop_single_pass()

        # The cancellation guard must have run via is_cancelled, not get_job
        assert mock_tracker.is_cancelled.call_count >= 1, (
            "is_cancelled must be called for the cancellation check"
        )
        is_cancelled_arg = mock_tracker.is_cancelled.call_args[0][0]
        assert is_cancelled_arg == _BACKFILL_JOB_ID, (
            f"is_cancelled called with {is_cancelled_arg!r}, expected {_BACKFILL_JOB_ID!r}"
        )

        # fail_job must NOT have been called (job was not cancelled)
        assert mock_tracker.fail_job.call_count == 0, (
            f"fail_job must not be called when job is not cancelled, "
            f"was called {mock_tracker.fail_job.call_count} times"
        )

    def test_no_active_backfill_skips_cancellation_check(self):
        """When _active_backfill_job_id is None, get_job and fail_job are not called."""
        mock_tracker = MagicMock()

        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=[],
        )
        # Leave _active_backfill_job_id as None (default)

        scheduler._run_loop_single_pass()

        get_job_count = mock_tracker.get_job.call_count
        assert get_job_count == 0, (
            f"get_job must not be called when no active backfill, "
            f"was called {get_job_count} times"
        )
        fail_job_count = mock_tracker.fail_job.call_count
        assert fail_job_count == 0, (
            f"fail_job must not be called when no active backfill, "
            f"was called {fail_job_count} times"
        )


# ---------------------------------------------------------------------------
# Codex Review Issue 1: JobTracker.is_cancelled reads DB directly (no cache)
# ---------------------------------------------------------------------------


def _make_db_with_background_jobs_table() -> str:
    """Create a temp SQLite DB with the background_jobs schema. Returns db path."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            progress_info TEXT,
            metadata TEXT
        )"""
        )
    return db_path


class TestJobTrackerIsCancelled:
    """
    Issue 1: JobTracker.is_cancelled(job_id) -> bool must read the DB cancelled
    column DIRECTLY, bypassing in-memory cache.

    After BackgroundJobManager writes cancelled=1 to the DB row (without touching
    JobTracker's in-memory state), is_cancelled must return True so the scheduler
    thread can observe the cancellation.
    """

    def setup_method(self):
        self.db_path = _make_db_with_background_jobs_table()

    def teardown_method(self):
        db_dir = os.path.dirname(self.db_path)
        shutil.rmtree(db_dir, ignore_errors=True)

    def test_is_cancelled_returns_false_for_running_job_not_cancelled_in_db(self):
        """is_cancelled returns False when cancelled=0 in DB."""
        tracker = JobTracker(self.db_path)
        tracker.register_job("job-ic-001", "lifecycle_backfill", "system")
        tracker.update_status("job-ic-001", status="running")

        result = tracker.is_cancelled("job-ic-001")

        assert result is False, (
            f"is_cancelled must return False for running non-cancelled job, got {result}"
        )

    def test_is_cancelled_returns_false_for_unknown_job(self):
        """is_cancelled returns False for a job_id that does not exist."""
        tracker = JobTracker(self.db_path)

        result = tracker.is_cancelled("nonexistent-job-id")

        assert result is False, (
            f"is_cancelled must return False for unknown job, got {result}"
        )

    def test_is_cancelled_reads_db_directly_bypassing_in_memory_cache(self):
        """
        Core Issue 1 test: is_cancelled must read the DB cancelled column directly.

        Scenario:
        1. Register job — enters _active_jobs (status=running, cancelled not set)
        2. Directly write cancelled=1 to the DB row, bypassing JobTracker's memory
        3. is_cancelled must return True (reads DB) even though in-memory job unchanged
        """
        tracker = JobTracker(self.db_path)
        tracker.register_job("job-ic-002", "lifecycle_backfill", "system")
        tracker.update_status("job-ic-002", status="running")

        # Confirm job is in memory with status=running
        with tracker._lock:
            in_memory_job = tracker._active_jobs.get("job-ic-002")
        assert in_memory_job is not None, "Job must be in active_jobs dict"
        assert in_memory_job.status == "running"

        # Directly write cancelled=1 to DB, simulating BackgroundJobManager cancel path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE background_jobs SET cancelled = 1 WHERE job_id = ?",
                ("job-ic-002",),
            )

        # is_cancelled must read DB directly and return True
        result = tracker.is_cancelled("job-ic-002")

        assert result is True, (
            "is_cancelled must return True after cancelled=1 written to DB directly. "
            f"Got {result!r}. This indicates is_cancelled reads in-memory cache "
            "(wrong) instead of querying the DB cancelled column directly."
        )


# ---------------------------------------------------------------------------
# Codex Review Issue 1 (scheduler side) + Issue 2b:
# scheduler uses is_cancelled (not get_job) and calls update_status('cancelled')
# ---------------------------------------------------------------------------


class TestSchedulerUsesIsCancelledNotGetJob:
    """
    Scheduler must call job_tracker.is_cancelled(job_id) NOT get_job(job_id),
    and on cancellation must call update_status(status='cancelled') not fail_job.
    """

    def test_scheduler_calls_is_cancelled_not_get_job_for_cancellation_check(self):
        """
        _run_loop_single_pass calls is_cancelled(active_backfill_job_id) and
        does NOT call get_job for the cancellation check.
        """
        mock_tracker = MagicMock()
        mock_tracker.is_cancelled.return_value = True

        mock_cli = Mock()
        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=[_make_stale_repo_record()],
            claude_cli_manager=mock_cli,
        )
        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        scheduler._run_loop_single_pass()

        # is_cancelled must be called with the backfill job_id
        is_cancelled_count = mock_tracker.is_cancelled.call_count
        assert is_cancelled_count >= 1, (
            f"is_cancelled must be called at least once. Was called {is_cancelled_count} times. "
            "Scheduler must use is_cancelled() for the cancellation check."
        )
        called_job_id = mock_tracker.is_cancelled.call_args[0][0]
        assert called_job_id == _BACKFILL_JOB_ID, (
            f"is_cancelled called with {called_job_id!r}, expected {_BACKFILL_JOB_ID!r}"
        )

        # get_job must NOT be called for the cancellation check (key Issue 1 assertion)
        get_job_count = mock_tracker.get_job.call_count
        assert get_job_count == 0, (
            f"get_job must NOT be called for the cancellation check. "
            f"Was called {get_job_count} times. "
            "Scheduler must use is_cancelled() instead of get_job().status check."
        )

        # Processing must stop when cancelled — submit_work must NOT be called
        assert mock_cli.submit_work.call_count == 0, (
            "submit_work must not be called when backfill is cancelled"
        )

    def test_cancellation_calls_update_status_cancelled_not_fail_job(self):
        """
        Issue 2b: cancellation must call update_status(status='cancelled'),
        not fail_job. A cancellation is not a failure.
        """
        mock_tracker = MagicMock()
        mock_tracker.is_cancelled.return_value = True

        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=[_make_stale_repo_record()],
        )
        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        scheduler._run_loop_single_pass()

        # fail_job must NOT be called for a cancellation
        assert mock_tracker.fail_job.call_count == 0, (
            f"fail_job must NOT be called for cancellation. "
            f"Was called {mock_tracker.fail_job.call_count} times. "
            "Use update_status(status='cancelled') instead."
        )

        # update_status must be called with status='cancelled'
        assert mock_tracker.update_status.call_count >= 1, (
            "update_status must be called to mark job as cancelled"
        )
        all_calls = mock_tracker.update_status.call_args_list
        cancelled_calls = [
            c
            for c in all_calls
            if c[1].get("status") == "cancelled"
            or (len(c[0]) > 1 and c[0][1] == "cancelled")
        ]
        assert len(cancelled_calls) >= 1, (
            f"update_status must be called with status='cancelled'. "
            f"Actual calls: {all_calls}"
        )


# ---------------------------------------------------------------------------
# Codex Review Issue 3: Conditional clear race condition
# ---------------------------------------------------------------------------


class TestConditionalClearRaceCondition:
    """
    Issue 3: _active_backfill_job_id must only be cleared when it still matches
    the job_id being cancelled. A concurrent new backfill cycle may have already
    replaced the id with a new one before the clear executes.

    We simulate the race using an is_cancelled side_effect: when called with
    old_job_id, it replaces _active_backfill_job_id with new_job_id (simulating
    the concurrent new backfill cycle), then returns True. The production cancel
    path runs and must use a conditional clear — only clear if current == old_job_id.
    Since by then it is new_job_id, new_job_id must survive.
    """

    def test_new_job_id_not_erased_when_race_condition_occurs(self):
        """
        Race condition: concurrent thread sets new_job_id during cancel processing.
        Production code must use conditional clear (current == cancelled_id) to
        preserve the new_job_id.
        """
        old_job_id = "old-backfill-job-id"
        new_job_id = "new-backfill-job-id"

        mock_tracker = MagicMock()

        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=[],
        )
        scheduler.set_active_backfill_job_id(old_job_id)

        def is_cancelled_side_effect(job_id):
            if job_id == old_job_id:
                # Simulate concurrent thread replacing id with new_job_id
                # BEFORE production cancel path runs the clear
                with scheduler._backfill_job_id_lock:
                    scheduler._active_backfill_job_id = new_job_id
                return True
            return False

        mock_tracker.is_cancelled.side_effect = is_cancelled_side_effect

        scheduler._run_loop_single_pass()

        # Unconditional clear: new_job_id would be erased (becomes None) — WRONG
        # Conditional clear: new_job_id survives because new_job_id != old_job_id — CORRECT
        with scheduler._backfill_job_id_lock:
            current_id = scheduler._active_backfill_job_id

        assert current_id == new_job_id, (
            f"Conditional clear must preserve new_job_id {new_job_id!r}. "
            f"Got {current_id!r}. Production code uses unconditional clear which "
            "erases the new id set by a concurrent backfill cycle."
        )

    def test_job_id_cleared_when_no_race_condition(self):
        """
        Normal case: when _active_backfill_job_id still matches the cancelled
        job, it must be cleared to None after cancellation processing.
        """
        mock_tracker = MagicMock()
        mock_tracker.is_cancelled.return_value = True

        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=[],
        )
        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        scheduler._run_loop_single_pass()

        with scheduler._backfill_job_id_lock:
            current_id = scheduler._active_backfill_job_id

        assert current_id is None, (
            f"_active_backfill_job_id must be cleared to None after cancellation "
            f"of the matching active job. Got {current_id!r}."
        )
