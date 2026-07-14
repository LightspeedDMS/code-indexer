"""
EVO-64385: a refresh already running on another cluster node must be SKIPPED,
not recorded as a failed job.

Every pod runs its own RefreshScheduler (it is not leader-gated), so when two
pods restart together their schedulers fire for the same repo at the same time.
Cross-pod dedup is enforced by the partial unique index idx_active_job_per_repo:
exactly one pod wins the insert, which is the intended design.

The loser, however, registered its in-flight refresh with the plain
JobTracker.register_job(), whose UniqueViolation escapes as a raw DB error and
propagates out of _execute_refresh(). BackgroundJobManager then marks the whole
global_repo_refresh job FAILED:

    duplicate key value violates unique constraint "idx_active_job_per_repo"
    DETAIL: Key (operation_type, repo_alias)=(global_repo_refresh, click-global)
            already exists.

A working dedup is thus reported as a failure, and because /health counts any
failed job, one benign race flips the server to "degraded" until the row ages
out. The loser must instead skip quietly: the repo IS being refreshed, by the
other node.

The tracker job id (``refresh-{alias}``) is deterministic and therefore SHARED
across pods, so the loser must also not complete_job()/fail_job() it -- that id
belongs to the winner's in-flight refresh.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.job_tracker import (
    DuplicateJobError as TrackerDuplicateJobError,
)


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def scheduler(tmp_path, golden_repos_dir):
    query_tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        job_tracker=MagicMock(),
    )


def _make_loser(scheduler):
    """Make this node lose the cluster race: the atomic insert reports a duplicate."""
    scheduler._job_tracker.register_job_if_no_conflict.side_effect = (
        TrackerDuplicateJobError(
            operation_type="global_repo_refresh",
            repo_alias="click-global",
            existing_job_id="refresh-click-global",
        )
    )


class TestDuplicateRefreshIsSkippedNotFailed:
    def test_losing_node_does_not_raise(self, scheduler):
        """The loser must return normally -- raising is what marks the job FAILED."""
        _make_loser(scheduler)

        result = scheduler._execute_refresh("click-global")

        assert result["success"] is True
        assert result.get("skipped") is True
        assert "another node" in result["message"].lower()

    def test_losing_node_does_not_touch_the_winners_tracker_job(self, scheduler):
        """refresh-{alias} is shared across pods: the loser must not complete/fail it."""
        _make_loser(scheduler)

        scheduler._execute_refresh("click-global")

        scheduler._job_tracker.complete_job.assert_not_called()
        scheduler._job_tracker.fail_job.assert_not_called()
        scheduler._job_tracker.update_status.assert_not_called()

    def test_losing_node_never_starts_the_refresh(self, scheduler):
        """The whole point: the repo is already being refreshed by the winner."""
        _make_loser(scheduler)

        with patch.object(scheduler.alias_manager, "read_alias") as read_alias:
            scheduler._execute_refresh("click-global")

        read_alias.assert_not_called()


class TestWinningNodeStillRefreshes:
    def test_winner_registers_atomically_then_runs_and_completes(self, scheduler):
        """Regression guard: the node that wins the race behaves exactly as before."""
        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            result = scheduler._execute_refresh("click-global")

        assert result["success"] is True

        tracker = scheduler._job_tracker
        # Registration must go through the cluster-atomic API, not the plain
        # register_job() whose UniqueViolation escapes as a raw DB error.
        tracker.register_job_if_no_conflict.assert_called_once()
        assert (
            tracker.register_job_if_no_conflict.call_args[0][0]
            == "refresh-click-global"
        )
        tracker.update_status.assert_called_once()
        tracker.complete_job.assert_called_once_with("refresh-click-global")
        tracker.fail_job.assert_not_called()

    def test_winner_still_fails_the_job_on_a_real_error(self, scheduler):
        """A genuine refresh error must still surface as a failure, not a skip."""
        with patch.object(
            scheduler.alias_manager,
            "read_alias",
            side_effect=RuntimeError("disk read error"),
        ):
            with pytest.raises(RuntimeError, match="disk read error"):
                scheduler._execute_refresh("click-global")

        scheduler._job_tracker.fail_job.assert_called_once()
        scheduler._job_tracker.complete_job.assert_not_called()
