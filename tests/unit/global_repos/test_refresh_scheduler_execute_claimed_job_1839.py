"""
Bug #1839: RefreshScheduler.execute_refresh_for_claimed_job() is the
execution-only entry point DistributedJobWorkerService must call for a
job it has already claimed, instead of trigger_refresh_for_repo() (which
in server mode SUBMITS a new job and collides with the caller's own
active row -- see test_distributed_job_worker_claimed_refresh_1839.py for
the full end-to-end reproduction using the real dedup machinery).

This file proves execute_refresh_for_claimed_job()'s own wiring in
isolation, following the exact pattern established for the sibling
EVO-64385 fix in test_refresh_scheduler_double_registration_64385.py: the
REAL _resolve_global_alias() and _execute_refresh()/_execute_refresh_impl()
run unmodified, with only the filesystem collaborator
(alias_manager.read_alias) and the alias registry patched, plus a
background_job_manager double whose submit_job() raises loudly if ever
called -- proving the method never re-enters submission.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.repositories.background_jobs import BackgroundJobManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


class _ExplodingBackgroundJobManager(BackgroundJobManager):
    """A REAL BackgroundJobManager (satisfying RefreshScheduler's
    Optional[BackgroundJobManager] type) whose submit_job must NEVER be
    called by execute_refresh_for_claimed_job() -- calling it fails the
    test loudly instead of silently re-entering the submission path."""

    def __init__(self) -> None:
        super().__init__(storage_path=None)

    def submit_job(self, *args: Any, **kwargs: Any) -> str:
        # Any is deliberate: this override always raises BEFORE inspecting
        # a single argument, so it must accept BackgroundJobManager.
        # submit_job's full concrete parameter set (operation_type, func,
        # submitter_username, is_admin, repo_alias, actor_username, lane,
        # snapshot_ctx, metadata, **kwargs) without retyping it --
        # narrowing the types here would add no safety, only upkeep.
        raise AssertionError(
            "execute_refresh_for_claimed_job() must not call submit_job() -- "
            "that re-enters the submission path this method exists to avoid "
            "(Bug #1839)."
        )


def _make_registry_with_repo(global_alias: str) -> MagicMock:
    """A registry double that only knows about one already-global alias,
    matching the pattern established in test_refresh_scheduler_resolve_alias.py."""
    registry = MagicMock()
    registry.get_global_repo = MagicMock(
        side_effect=lambda alias: {"alias_name": global_alias}
        if alias == global_alias
        else None
    )
    return registry


def _make_scheduler(tmp_path, golden_repos_dir, registry):
    query_tracker = QueryTracker()
    tracker = MagicMock()
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        job_tracker=tracker,
        background_job_manager=_ExplodingBackgroundJobManager(),
        registry=registry,
    )
    scheduler._job_tracker = tracker
    return scheduler


class TestExecuteRefreshForClaimedJobRunsTheRealRefresh:
    """No submission -- the refresh WORK runs directly, tracked_by_caller."""

    def test_resolves_bare_alias_and_runs_refresh_without_submitting(
        self, tmp_path, golden_repos_dir
    ):
        """Bare alias "click" resolves to "click-global" and the real
        _execute_refresh_impl() reads that exact global alias -- proving
        resolution ran, and that no submission happened (the exploding
        background_job_manager would otherwise have raised)."""
        scheduler = _make_scheduler(
            tmp_path,
            golden_repos_dir,
            registry=_make_registry_with_repo("click-global"),
        )

        with patch.object(
            scheduler.alias_manager, "read_alias", return_value=None
        ) as read_alias:
            result = scheduler.execute_refresh_for_claimed_job("click")

        read_alias.assert_called_once_with("click-global")
        assert result == {
            "success": True,
            "alias": "click-global",
            "message": "Alias not found, skipped",
        }

    def test_tracked_by_caller_skips_second_tracker_registration(
        self, tmp_path, golden_repos_dir
    ):
        """EVO-64385: the caller (DistributedJobWorkerService) already owns
        the idx_active_job_per_repo slot for this job -- execute_refresh_for_
        claimed_job() must pass tracked_by_caller=True through to
        _execute_refresh(), so the JobTracker is never re-registered here."""
        scheduler = _make_scheduler(
            tmp_path,
            golden_repos_dir,
            registry=_make_registry_with_repo("click-global"),
        )

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            scheduler.execute_refresh_for_claimed_job("click-global")

        scheduler._job_tracker.register_job.assert_not_called()
        scheduler._job_tracker.complete_job.assert_not_called()
        scheduler._job_tracker.fail_job.assert_not_called()


class TestExecuteRefreshForClaimedJobAliasResolutionErrors:
    """Alias resolution failures propagate exactly like trigger_refresh_for_repo()."""

    def test_alias_not_found_raises_value_error(self, tmp_path, golden_repos_dir):
        scheduler = _make_scheduler(
            tmp_path,
            golden_repos_dir,
            registry=_make_registry_with_repo("some-other-repo-global"),
        )

        with pytest.raises(ValueError, match="not found in global registry"):
            scheduler.execute_refresh_for_claimed_job("nonexistent-repo")
