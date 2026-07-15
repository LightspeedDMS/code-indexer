"""
EVO-64385: one logical refresh must claim the active-job slot exactly ONCE.

In cluster (postgres) mode, scheduled global-repo refresh was failing on EVERY
run -- last_refresh had been frozen since 2026-07-10 on the test cluster -- with:

    duplicate key value violates unique constraint "idx_active_job_per_repo"
    DETAIL: Key (operation_type, repo_alias)=(global_repo_refresh, click-global)
            already exists.

The duplicate is the job's OWN parent row. Two guards, added by two different
bugs, overlap:

  1. BackgroundJobManager.submit_job() (Bug #1065) calls
     register_job_if_no_conflict(job_id=<uuid>, "global_repo_refresh", <alias>),
     which INSERTs the row that occupies the idx_active_job_per_repo slot.
  2. The worker then runs _execute_refresh(), which (Bug #935) registers a
     SECOND JobTracker job ("refresh-<alias>") for the SAME
     (operation_type, repo_alias) pair -- and collides with row 1.

The partial unique index rejects the second insert, the raw DB error escapes
_execute_refresh(), and BackgroundJobManager marks the job FAILED. The refresh
work never runs at all.

Fix: the outer submit_job row IS the cluster-visible active job that Bug #935
wanted drain-status to see, so a refresh running under a BackgroundJobManager
job must not register its own tracker job (nor complete/fail it -- that is the
outer job's lifecycle). A refresh invoked DIRECTLY (CLI, refresh_repo(),
exit_write_mode) has no outer job and must still register, exactly as before.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


def _make_scheduler(tmp_path, golden_repos_dir, background_job_manager=None):
    query_tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        job_tracker=MagicMock(),
        background_job_manager=background_job_manager,
    )


class _FakeBackgroundJobManager:
    """Stands in for BackgroundJobManager: claims the slot, then runs the worker.

    submit_job() is where the FIRST (and only legitimate) registration of
    (global_repo_refresh, alias) happens -- mirroring Bug #1065's atomic claim.
    """

    def __init__(self, job_tracker):
        self._job_tracker = job_tracker
        self.claimed = []

    def submit_job(self, operation_type, func, repo_alias=None, **kwargs):
        # The atomic claim that takes the idx_active_job_per_repo slot.
        self.claimed.append((operation_type, repo_alias))
        self._job_tracker.register_job_if_no_conflict(
            job_id="job-uuid-1",
            operation_type=operation_type,
            username="system",
            repo_alias=repo_alias,
        )
        func()  # run the worker inline, as the real manager's thread would
        return "job-uuid-1"


class TestRefreshUnderAnOuterJobRegistersOnlyOnce:
    def test_worker_does_not_register_a_second_tracker_job(
        self, tmp_path, golden_repos_dir
    ):
        """The submit_job claim already owns the slot; the worker must not re-claim it."""
        tracker = MagicMock()
        mgr = _FakeBackgroundJobManager(tracker)
        scheduler = _make_scheduler(
            tmp_path, golden_repos_dir, background_job_manager=mgr
        )
        scheduler._job_tracker = tracker

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            scheduler._submit_refresh_job("click-global")

        # submit_job claimed the slot exactly once...
        assert mgr.claimed == [("global_repo_refresh", "click-global")]
        assert tracker.register_job_if_no_conflict.call_count == 1
        # ...and the worker did NOT insert a second row for the same pair.
        tracker.register_job.assert_not_called()

    def test_worker_leaves_job_lifecycle_to_the_outer_job(
        self, tmp_path, golden_repos_dir
    ):
        """complete_job/fail_job on 'refresh-<alias>' belong to the outer job, not us."""
        tracker = MagicMock()
        mgr = _FakeBackgroundJobManager(tracker)
        scheduler = _make_scheduler(
            tmp_path, golden_repos_dir, background_job_manager=mgr
        )
        scheduler._job_tracker = tracker

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            scheduler._submit_refresh_job("click-global")

        tracker.complete_job.assert_not_called()
        tracker.fail_job.assert_not_called()
        tracker.update_status.assert_not_called()

    def test_the_refresh_work_actually_runs(self, tmp_path, golden_repos_dir):
        """The whole point: the refresh must EXECUTE, not be skipped or failed.

        The reverted first attempt at EVO-64385 made every node stand down, so
        no refresh ran at all. Assert the work is reached.
        """
        tracker = MagicMock()
        mgr = _FakeBackgroundJobManager(tracker)
        scheduler = _make_scheduler(
            tmp_path, golden_repos_dir, background_job_manager=mgr
        )
        scheduler._job_tracker = tracker

        with patch.object(
            scheduler.alias_manager, "read_alias", return_value=None
        ) as read_alias:
            scheduler._submit_refresh_job("click-global")

        read_alias.assert_called_once_with("click-global")


class TestDirectRefreshStillRegistersItself:
    """No outer job (CLI, refresh_repo(), exit_write_mode) -> Bug #935 still applies."""

    def test_direct_execute_registers_and_completes(self, tmp_path, golden_repos_dir):
        scheduler = _make_scheduler(
            tmp_path, golden_repos_dir, background_job_manager=None
        )
        tracker = scheduler._job_tracker

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            result = scheduler._execute_refresh("click-global")

        assert result["success"] is True
        tracker.register_job.assert_called_once()
        assert tracker.register_job.call_args[0][0] == "refresh-click-global"
        tracker.update_status.assert_called_once()
        tracker.complete_job.assert_called_once_with("refresh-click-global")
        tracker.fail_job.assert_not_called()

    def test_direct_execute_fails_the_tracker_job_on_error(
        self, tmp_path, golden_repos_dir
    ):
        scheduler = _make_scheduler(
            tmp_path, golden_repos_dir, background_job_manager=None
        )
        tracker = scheduler._job_tracker

        with patch.object(
            scheduler.alias_manager,
            "read_alias",
            side_effect=RuntimeError("disk read error"),
        ):
            with pytest.raises(RuntimeError, match="disk read error"):
                scheduler._execute_refresh("click-global")

        tracker.fail_job.assert_called_once()
        tracker.complete_job.assert_not_called()
