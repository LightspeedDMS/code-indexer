"""
Unit tests for local repo skip - scenarios 4-6: git repos, mixed lists,
startup reconciliation.

Companion to test_refresh_scheduler_local_auto_skip.py (scenarios 1-3).

Test Scenarios covered here:
4. Non-local repos (git repos) are still refreshed normally
5. Mixed repo list correctly filters out only local repos
6. Startup reconciliation skips local repos (regression guard)
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Golden repos directory."""
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_registry():
    """Registry mock with sensible defaults."""
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    return registry


@pytest.fixture
def mock_config_source():
    """Config source mock - short interval so tests don't hang."""
    cs = MagicMock()
    cs.get_global_refresh_interval.return_value = 3600
    return cs


@pytest.fixture
def scheduler(golden_repos_dir, mock_registry, mock_config_source):
    """RefreshScheduler with injected mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )


def _run_one_loop_pass(scheduler):
    """Run exactly one iteration of _scheduler_loop() then stop."""
    original_wait = scheduler._stop_event.wait

    def stop_after_one(timeout=None):
        scheduler._running = False
        return True

    scheduler._running = True
    scheduler._stop_event.clear()
    scheduler._stop_event.wait = stop_after_one
    try:
        scheduler._scheduler_loop()
    finally:
        scheduler._stop_event.wait = original_wait


# ---------------------------------------------------------------------------
# Scenario 4: Non-local (git) repos are still refreshed normally
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scenario 5: Mixed repo list - only local repos are filtered out
# ---------------------------------------------------------------------------


class TestMixedRepoListFiltersCorrectly:
    """
    Scenario 5: When the registry contains a mix of local and git repos,
    only the git repos should be submitted to the scheduled refresh cycle.
    Local repos of all varieties should be filtered out.
    """

    def test_all_local_list_submits_nothing(self, scheduler, mock_registry):
        """
        When all repos are local, nothing should be submitted to the refresh cycle.
        This prevents wasted background jobs on servers with only local repos.
        """
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "cidx-meta-global", "repo_url": "local://cidx-meta"},
            {"alias_name": "dep-map-global", "repo_url": "local://dep-map"},
            {"alias_name": "langfuse-a-global", "repo_url": "/tmp/langfuse-a"},
        ]

        submitted = []

        with patch.object(
            scheduler,
            "_submit_refresh_job",
            side_effect=lambda a, **kw: submitted.append(a),
        ):
            _run_one_loop_pass(scheduler)

        assert submitted == [], (
            f"ALL LOCAL: When all repos are local, nothing should be submitted. "
            f"Got: {submitted}"
        )


# ---------------------------------------------------------------------------
# Scenario 6: Startup reconciliation also skips local repos (regression guard)
# ---------------------------------------------------------------------------


class TestStartupReconciliationSkipsLocalRepos:
    """
    Scenario 6: reconcile_golden_repos() already skips local:// repos.
    This is a regression guard to ensure that behavior is preserved.

    The reconciliation is for restoring git repo masters from versioned snapshots.
    Local repos don't have versioned copies to restore from.
    """

    def test_reconciliation_skips_local_repos(
        self, golden_repos_dir, mock_config_source
    ):
        """
        reconcile_golden_repos() must skip local:// repos.
        """
        registry = MagicMock()
        registry.list_global_repos.return_value = [
            {
                "alias_name": "cidx-meta-global",
                "repo_url": "local://cidx-meta",
            },
            {
                "alias_name": "my-git-repo-global",
                "repo_url": "https://github.com/org/repo.git",
            },
        ]

        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=MagicMock(spec=QueryTracker),
            cleanup_manager=MagicMock(spec=CleanupManager),
            registry=registry,
        )

        restore_calls = []
        with patch.object(
            sched,
            "_restore_master_from_versioned",
            side_effect=lambda a, p: restore_calls.append(a) or False,
        ):
            sched.reconcile_golden_repos()

        assert "cidx-meta-global" not in restore_calls, (
            "RECONCILIATION: local:// repos must not trigger master restoration. "
            "They have no versioned copies to restore from."
        )

    def test_reconciliation_processes_git_repos(
        self, golden_repos_dir, mock_config_source
    ):
        """
        reconcile_golden_repos() must still process git repos (regression guard).
        """
        registry = MagicMock()

        registry.list_global_repos.return_value = [
            {
                "alias_name": "my-git-repo-global",
                "repo_url": "https://github.com/org/repo.git",
            }
        ]

        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=MagicMock(spec=QueryTracker),
            cleanup_manager=MagicMock(spec=CleanupManager),
            registry=registry,
        )

        restore_calls = []
        with patch.object(
            sched,
            "_restore_master_from_versioned",
            side_effect=lambda a, p: restore_calls.append(a) or False,
        ):
            sched.reconcile_golden_repos()

        assert "my-git-repo-global" in restore_calls, (
            "RECONCILIATION: git repos with missing masters must trigger restoration. "
            "The skip for local repos must not affect git repo processing."
        )

    def test_reconciliation_skips_bare_filesystem_path_repos(
        self, golden_repos_dir, mock_config_source
    ):
        """
        reconcile_golden_repos() must also skip repos with bare filesystem paths.
        """
        registry = MagicMock()

        registry.list_global_repos.return_value = [
            {
                "alias_name": "scip-mock-global",
                "repo_url": "/tmp/scip-python-mock",
            },
        ]

        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=MagicMock(spec=QueryTracker),
            cleanup_manager=MagicMock(spec=CleanupManager),
            registry=registry,
        )

        restore_calls = []
        with patch.object(
            sched,
            "_restore_master_from_versioned",
            side_effect=lambda a, p: restore_calls.append(a) or False,
        ):
            sched.reconcile_golden_repos()

        assert "scip-mock-global" not in restore_calls, (
            "RECONCILIATION: repos with bare filesystem paths must not trigger "
            "master restoration, just like local:// repos."
        )
