"""
Unit tests for Bug #1066: RefreshScheduler must NOT advance next_refresh when
_submit_refresh_job raises a generic Exception (transient failure).

Policy:
- SUCCESS             -> advance next_refresh (Story #284 AC1)
- DuplicateJobError   -> advance next_refresh (in-flight job; expected)
- Generic Exception   -> leave next_refresh UNCHANGED (retry on next poll)

The tests drive _scheduler_loop in a controlled background thread using a mock
registry. They patch _submit_refresh_job to simulate each outcome, stop the
loop after exactly one processing pass, and assert whether update_next_refresh
was called.
"""

import threading
import time
from unittest.mock import MagicMock, patch


from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.repositories.background_jobs import DuplicateJobError


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOW = time.time()  # captured at import; overdue repos use _NOW - 1


def _make_repo(alias_name: str, repo_url: str = "https://example.com/repo.git") -> dict:
    """Return a minimal global-repo dict that is overdue for refresh."""
    return {
        "alias_name": alias_name,
        "repo_url": repo_url,
        "next_refresh": str(_NOW - 1),  # one second in the past → due
    }


def _configure_mock_registry_due(mock_registry: MagicMock, repos: list) -> None:
    """Wire mock_registry so list_due_repos returns the given repos.

    The refactored _scheduler_loop calls list_due_repos(limit=..., now=...) instead
    of list_global_repos() + manual filtering. We keep list_global_repos returning
    the same repos so the unscheduled-spread pass can also operate if needed.
    """
    mock_registry.list_global_repos.return_value = repos
    # list_due_repos must honour the call: return repos regardless of limit/now
    # so the loop actually processes them in tests.
    mock_registry.list_due_repos.return_value = repos


def _make_scheduler(
    tmp_path,
    mock_registry: MagicMock,
    mock_submit_side_effect=None,
) -> RefreshScheduler:
    """
    Build a RefreshScheduler wired to a mock registry.

    Returns (scheduler, mock_update_next_refresh_callable).
    mock_submit_side_effect controls what _submit_refresh_job does:
      - None          → returns None (success)
      - An exception  → raises that exception
    """
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)

    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=mock_registry,
    )

    return scheduler


def _run_one_iteration(scheduler: RefreshScheduler) -> None:
    """
    Run _scheduler_loop for exactly one processing pass then stop.

    Starts the loop in a background thread. After _submit_refresh_job is called
    (or not called within a timeout), stops the loop via stop_event and joins.

    Uses a short poll interval by patching _calculate_poll_interval to return 0.
    """
    scheduler._running = True
    scheduler._stop_event.clear()

    # Stop after one pass: once the loop reaches the sleep at the end of
    # the first iteration, we signal stop.  We also patch the poll sleep to
    # return immediately.
    original_wait = scheduler._stop_event.wait

    call_count = [0]

    def _one_shot_wait(timeout=None):
        call_count[0] += 1
        if call_count[0] >= 1:
            scheduler._running = False
            scheduler._stop_event.set()
            return True  # simulate "stop event fired"
        return original_wait(timeout=timeout)

    with patch.object(scheduler._stop_event, "wait", side_effect=_one_shot_wait):
        thread = threading.Thread(target=scheduler._scheduler_loop, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

    assert not thread.is_alive(), "Scheduler loop did not finish within 5 seconds"


# ---------------------------------------------------------------------------
# Bug regression: generic Exception must NOT advance next_refresh
# ---------------------------------------------------------------------------


class TestGenericExceptionDoesNotAdvanceNextRefresh:
    """Regression guard for Bug #1066: failed submit must not advance next_refresh."""

    def test_update_next_refresh_not_called_on_submit_exception(self, tmp_path):
        """
        When _submit_refresh_job raises a generic Exception,
        registry.update_next_refresh MUST NOT be called for that alias.

        Before the fix, update_next_refresh was called unconditionally,
        advancing the repo by a full interval and skipping one refresh cycle.
        """
        alias = "my-repo-global"
        mock_registry = MagicMock()
        _configure_mock_registry_due(mock_registry, [_make_repo(alias)])

        scheduler = _make_scheduler(tmp_path, mock_registry)

        boom = RuntimeError("transient network error")

        with (
            patch.object(scheduler, "cleanup_stale_write_mode_markers"),
            patch.object(scheduler, "get_refresh_interval", return_value=3600),
            patch.object(scheduler, "_submit_refresh_job", side_effect=boom),
        ):
            _run_one_iteration(scheduler)

        # update_next_refresh must NOT have been called
        mock_registry.update_next_refresh.assert_not_called()

    def test_multiple_repos_only_failed_one_is_not_advanced(self, tmp_path):
        """
        When only one of two repos fails, only the failing repo is not advanced;
        the successful repo still gets next_refresh updated.
        """
        alias_ok = "ok-repo-global"
        alias_fail = "fail-repo-global"
        mock_registry = MagicMock()
        _configure_mock_registry_due(
            mock_registry,
            [_make_repo(alias_ok), _make_repo(alias_fail)],
        )

        scheduler = _make_scheduler(tmp_path, mock_registry)

        def _selective_submit(alias_name):
            if alias_name == alias_fail:
                raise RuntimeError("disk failure")
            return "job-ok"

        with (
            patch.object(scheduler, "cleanup_stale_write_mode_markers"),
            patch.object(scheduler, "get_refresh_interval", return_value=3600),
            patch.object(
                scheduler, "_submit_refresh_job", side_effect=_selective_submit
            ),
        ):
            _run_one_iteration(scheduler)

        # ok-repo → advanced
        calls = [c[0][0] for c in mock_registry.update_next_refresh.call_args_list]
        assert alias_ok in calls, f"Expected {alias_ok} to be advanced"
        assert alias_fail not in calls, f"Expected {alias_fail} NOT to be advanced"


# ---------------------------------------------------------------------------
# Success path: next_refresh IS advanced
# ---------------------------------------------------------------------------


class TestSuccessAdvancesNextRefresh:
    """On successful submit, update_next_refresh must be called (Story #284 AC1)."""

    def test_update_next_refresh_called_on_success(self, tmp_path):
        """
        When _submit_refresh_job returns without raising,
        registry.update_next_refresh MUST be called with a future timestamp.
        """
        alias = "healthy-repo-global"
        mock_registry = MagicMock()
        _configure_mock_registry_due(mock_registry, [_make_repo(alias)])

        scheduler = _make_scheduler(tmp_path, mock_registry)

        with (
            patch.object(scheduler, "cleanup_stale_write_mode_markers"),
            patch.object(scheduler, "get_refresh_interval", return_value=3600),
            patch.object(scheduler, "_submit_refresh_job", return_value="job-abc"),
        ):
            _run_one_iteration(scheduler)

        mock_registry.update_next_refresh.assert_called_once()
        call_alias, new_ts = mock_registry.update_next_refresh.call_args[0]
        assert call_alias == alias
        assert new_ts > _NOW, "next_refresh must be advanced into the future"

    def test_next_refresh_advanced_by_approximately_interval(self, tmp_path):
        """
        The new next_refresh should be roughly now + interval (within jitter bounds).
        Jitter is +/- 10% so the new value must be in [interval*0.9, interval*1.1] from now.
        """
        alias = "timed-repo-global"
        interval = 3600
        mock_registry = MagicMock()
        _configure_mock_registry_due(mock_registry, [_make_repo(alias)])

        scheduler = _make_scheduler(tmp_path, mock_registry)

        before = time.time()
        with (
            patch.object(scheduler, "cleanup_stale_write_mode_markers"),
            patch.object(scheduler, "get_refresh_interval", return_value=interval),
            patch.object(scheduler, "_submit_refresh_job", return_value="job-xyz"),
        ):
            _run_one_iteration(scheduler)
        after = time.time()

        _, new_ts = mock_registry.update_next_refresh.call_args[0]
        low = before + interval * 0.9
        high = after + interval * 1.1
        assert low <= new_ts <= high, (
            f"Expected next_refresh in [{low:.0f}, {high:.0f}], got {new_ts:.0f}"
        )


# ---------------------------------------------------------------------------
# DuplicateJobError: next_refresh IS advanced (in-flight job, acceptable)
# ---------------------------------------------------------------------------


class TestDuplicateJobErrorAdvancesNextRefresh:
    """
    DuplicateJobError means a prior refresh is still running.
    Advancing next_refresh is correct — the in-flight job will complete
    and a fresh cycle will start on the new schedule.
    """

    def test_update_next_refresh_called_on_duplicate_job_error(self, tmp_path):
        """
        When _submit_refresh_job raises DuplicateJobError,
        registry.update_next_refresh MUST still be called.
        """
        alias = "busy-repo-global"
        mock_registry = MagicMock()
        _configure_mock_registry_due(mock_registry, [_make_repo(alias)])

        scheduler = _make_scheduler(tmp_path, mock_registry)

        with (
            patch.object(scheduler, "cleanup_stale_write_mode_markers"),
            patch.object(scheduler, "get_refresh_interval", return_value=3600),
            patch.object(
                scheduler,
                "_submit_refresh_job",
                side_effect=DuplicateJobError(
                    "global_repo_refresh", "busy-repo-global", "job-123"
                ),
            ),
        ):
            _run_one_iteration(scheduler)

        mock_registry.update_next_refresh.assert_called_once()
        call_alias, new_ts = mock_registry.update_next_refresh.call_args[0]
        assert call_alias == alias
        assert new_ts > _NOW


# ---------------------------------------------------------------------------
# Not-due repos: update_next_refresh never called for them
# ---------------------------------------------------------------------------


class TestNotDueRepoNotAdvanced:
    """Repos whose next_refresh is in the future must not be touched."""

    def test_not_due_repo_is_not_advanced(self, tmp_path):
        """
        A repo with next_refresh in the future is skipped entirely;
        update_next_refresh must not be called for it.

        In the refactored loop, list_due_repos() only returns repos that are due.
        A future repo never appears in that list, so _submit_refresh_job is never
        called and update_next_refresh is never called.
        """
        alias = "future-repo-global"
        future_repo = {
            "alias_name": alias,
            "repo_url": "https://example.com/repo.git",
            "next_refresh": str(_NOW + 9999),  # far in the future
        }
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [future_repo]
        # list_due_repos correctly returns empty for a future repo
        mock_registry.list_due_repos.return_value = []

        scheduler = _make_scheduler(tmp_path, mock_registry)

        with (
            patch.object(scheduler, "cleanup_stale_write_mode_markers"),
            patch.object(scheduler, "get_refresh_interval", return_value=3600),
            patch.object(
                scheduler, "_submit_refresh_job", return_value="job-never"
            ) as mock_submit,
        ):
            _run_one_iteration(scheduler)

        mock_registry.update_next_refresh.assert_not_called()
        mock_submit.assert_not_called()
