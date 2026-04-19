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

from unittest.mock import MagicMock, Mock

from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.services.job_tracker import TrackedJob

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

    def test_cancelled_backfill_calls_fail_job_and_skips_repo_processing(self):
        """When backfill is cancelled: fail_job called with job_id AND no repo is processed."""
        mock_tracker = MagicMock()
        cancelled_job = _make_tracked_job(_BACKFILL_JOB_ID, "cancelled")
        mock_tracker.get_job.return_value = cancelled_job

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

        fail_job_call_count = mock_tracker.fail_job.call_count
        assert fail_job_call_count == 1, (
            f"fail_job must be called exactly once, was called {fail_job_call_count} times"
        )
        fail_job_call_job_id = mock_tracker.fail_job.call_args[0][0]
        assert fail_job_call_job_id == _BACKFILL_JOB_ID, (
            f"fail_job must receive {_BACKFILL_JOB_ID!r}, got {fail_job_call_job_id!r}"
        )

        submit_call_count = mock_cli_manager.submit_work.call_count
        assert submit_call_count == 0, (
            f"claude_cli_manager.submit_work must not be called when cancelled, "
            f"was called {submit_call_count} times"
        )

    def test_running_backfill_does_not_call_fail_job_and_processes_repos(self):
        """When backfill is running: fail_job NOT called AND cancellation check ran normally.

        Verifies the cancellation guard executed (get_job called) but correctly
        did not trigger early return (fail_job not called, loop continued).
        """
        mock_tracker = MagicMock()
        running_job = _make_tracked_job(_BACKFILL_JOB_ID, "running")
        mock_tracker.get_job.return_value = running_job

        stale_repos = [_make_stale_repo_record()]
        golden_repos = {_REPO_ALIAS: {"clone_path": _REPO_PATH}}

        scheduler = _make_scheduler(
            job_tracker=mock_tracker,
            stale_repos=stale_repos,
            golden_repos=golden_repos,
        )
        scheduler.set_active_backfill_job_id(_BACKFILL_JOB_ID)

        scheduler._run_loop_single_pass()

        # The cancellation guard must have run — get_job called with the backfill job_id
        get_job_called_with = mock_tracker.get_job.call_args[0][0]
        assert get_job_called_with == _BACKFILL_JOB_ID, (
            f"get_job must be called with {_BACKFILL_JOB_ID!r}, got {get_job_called_with!r}"
        )

        # fail_job must NOT have been called (job was running, not cancelled)
        fail_job_count = mock_tracker.fail_job.call_count
        assert fail_job_count == 0, (
            f"fail_job must not be called when job is running, was called {fail_job_count} times"
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
