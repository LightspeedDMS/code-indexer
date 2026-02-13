"""
Unit tests for RefreshScheduler skipping local:// repos.

Tests that local:// repos (like cidx-meta-global) are skipped both:
1. In _scheduler_loop() before job submission (avoids phantom Running jobs)
2. In _execute_refresh() before expensive reconciliation operations

This prevents local repos from appearing as "Running" or "Pending" in the
dashboard when they will be immediately skipped anyway.
"""

from unittest.mock import patch, MagicMock

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


class TestRefreshSchedulerLocalRepoSkip:
    """Test suite for local:// repo skip logic in RefreshScheduler."""

    @pytest.fixture
    def golden_repos_dir(self, tmp_path):
        """Create a golden repos directory structure."""
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)
        return golden_repos_dir

    @pytest.fixture
    def config_mgr(self, tmp_path):
        """Create a ConfigManager instance."""
        return ConfigManager(tmp_path / ".code-indexer" / "config.json")

    @pytest.fixture
    def query_tracker(self):
        """Create a QueryTracker instance."""
        return QueryTracker()

    @pytest.fixture
    def cleanup_manager(self, query_tracker):
        """Create a CleanupManager instance."""
        return CleanupManager(query_tracker)

    @pytest.fixture
    def registry(self, golden_repos_dir):
        """Create a GlobalRegistry instance."""
        return GlobalRegistry(str(golden_repos_dir))

    @pytest.fixture
    def alias_manager(self, golden_repos_dir):
        """Create an AliasManager instance."""
        return AliasManager(str(golden_repos_dir / "aliases"))

    def test_scheduler_loop_skips_local_repos_before_job_submission(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        alias_manager,
    ):
        """
        Test that _scheduler_loop() skips local:// repos before calling _submit_refresh_job().

        This prevents phantom "Running" or "Pending" jobs appearing in the dashboard
        for local repos that will be immediately skipped.

        Setup:
        - Registry with 2 repos: one local:// (cidx-meta-global), one remote (test-repo-global)

        Expected:
        - _submit_refresh_job() called ONLY for remote repo
        - _submit_refresh_job() NOT called for local repo
        """
        # Setup: Create one local repo and one remote repo
        local_repo_dir = golden_repos_dir / "cidx-meta"
        local_repo_dir.mkdir()
        alias_manager.create_alias("cidx-meta-global", str(local_repo_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            "local://cidx-meta",  # local:// URL
            str(local_repo_dir),
            allow_reserved=True,
        )

        remote_repo_dir = golden_repos_dir / "test-repo"
        remote_repo_dir.mkdir()
        alias_manager.create_alias("test-repo-global", str(remote_repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",  # Remote git URL
            str(remote_repo_dir),
        )

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
        )

        # Mock _submit_refresh_job to track calls and stop loop after one iteration
        def stop_after_call(alias_name):
            scheduler._running = False

        with patch.object(
            scheduler, "_submit_refresh_job", side_effect=stop_after_call
        ) as mock_submit, patch.object(
            scheduler, "get_refresh_interval", return_value=0
        ):
            # Run one iteration of the actual scheduler loop
            scheduler._running = True
            scheduler._scheduler_loop()

            # Verify: _submit_refresh_job called ONLY for remote repo
            assert mock_submit.call_count == 1, (
                f"Expected 1 call (remote repo only), got {mock_submit.call_count}. "
                "Local repos should be skipped before job submission."
            )
            # Verify the call was for the remote repo, not the local one
            call_args = mock_submit.call_args[0]
            assert call_args[0] == "test-repo-global", (
                f"Expected call for 'test-repo-global', got '{call_args[0]}'"
            )

    def test_execute_refresh_skips_local_repo_before_reconciliation(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        alias_manager,
    ):
        """
        Test that _execute_refresh() skips local:// repos BEFORE expensive operations.

        The local:// check must occur before _detect_existing_indexes() and
        _reconcile_registry_with_filesystem() to avoid unnecessary filesystem
        scanning for repos that cannot be refreshed.

        Expected:
        - Returns {"success": True, "message": "Local repo, skipped"}
        - _detect_existing_indexes() NOT called
        - _reconcile_registry_with_filesystem() NOT called
        """
        # Setup: Create local repo
        local_repo_dir = golden_repos_dir / "cidx-meta"
        local_repo_dir.mkdir()
        alias_manager.create_alias("cidx-meta-global", str(local_repo_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            "local://cidx-meta",  # local:// URL
            str(local_repo_dir),
            allow_reserved=True,
        )

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
        )

        # Mock expensive operations to verify they're NOT called
        with patch.object(
            scheduler, "_detect_existing_indexes"
        ) as mock_detect, patch.object(
            scheduler, "_reconcile_registry_with_filesystem"
        ) as mock_reconcile:
            # Execute: Refresh local repo
            result = scheduler._execute_refresh("cidx-meta-global")

            # Verify: Returns skip message
            assert result["success"] is True
            assert result["alias"] == "cidx-meta-global"
            assert "Local repo" in result["message"] or "skipped" in result["message"]

            # Verify: Expensive operations NOT called
            assert mock_detect.call_count == 0, (
                "_detect_existing_indexes() should NOT be called for local repos. "
                "Check should happen BEFORE reconciliation."
            )
            assert mock_reconcile.call_count == 0, (
                "_reconcile_registry_with_filesystem() should NOT be called for local repos. "
                "Check should happen BEFORE reconciliation."
            )

    def test_execute_refresh_processes_remote_repos_normally(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        alias_manager,
    ):
        """
        Test that non-local repos still go through full refresh flow (regression test).

        Ensures the local:// skip logic doesn't break normal repo processing.

        Expected:
        - Remote repos still reach reconciliation code
        - _detect_existing_indexes() IS called for remote repos
        """
        # Setup: Create remote repo
        remote_repo_dir = golden_repos_dir / "test-repo"
        remote_repo_dir.mkdir()
        alias_manager.create_alias("test-repo-global", str(remote_repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",  # Remote git URL
            str(remote_repo_dir),
        )

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
        )

        # Mock GitPullUpdater to avoid actual git operations
        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False  # No changes = skip refresh
            mock_updater_cls.return_value = mock_updater

            # Track if _detect_existing_indexes is called
            with patch.object(scheduler, "_detect_existing_indexes") as mock_detect:
                mock_detect.return_value = {
                    "semantic": True,
                    "fts": True,
                    "temporal": False,
                    "scip": False,
                }

                # Execute: Refresh remote repo
                result = scheduler._execute_refresh("test-repo-global")

                # Verify: Reconciliation IS called for remote repos
                assert mock_detect.call_count >= 1, (
                    "_detect_existing_indexes() SHOULD be called for remote repos. "
                    "Local skip logic should not affect remote repos."
                )

                # Verify: Did not return early with skip message
                assert "Local repo" not in result.get("message", "")
