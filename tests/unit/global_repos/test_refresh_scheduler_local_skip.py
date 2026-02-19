"""
Unit tests for RefreshScheduler handling of local:// repos (Story #224 updated).

After Story #224 (C1, C2, C3):
- Local repos ARE submitted to _submit_refresh_job (C1: skip block removed)
- Local repos go through reconciliation but use mtime detection, not early return (C2)
- Local repos use live directory as source_path, not versioned snapshot (C3)

These tests replace the old "skip" tests that verified behavior removed in Story #224.
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
    """Test suite for local:// repo handling in RefreshScheduler (Story #224)."""

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

    def test_scheduler_loop_submits_both_local_and_remote_repos(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        alias_manager,
    ):
        """
        C1 (Story #224): _scheduler_loop() must submit BOTH local and remote repos.

        Previously local:// repos were skipped before job submission. After C1
        that skip is removed — local repos are submitted to _submit_refresh_job()
        just like remote repos. The RefreshScheduler now handles versioned
        indexing for local repos via mtime detection.

        Setup:
        - Registry with 2 repos: one local:// (cidx-meta-global), one remote (test-repo-global)

        Expected:
        - _submit_refresh_job() called for BOTH repos
        """
        # Setup: Create one local repo and one remote repo
        local_repo_dir = golden_repos_dir / "cidx-meta"
        local_repo_dir.mkdir()
        alias_manager.create_alias("cidx-meta-global", str(local_repo_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            "local://cidx-meta",
            str(local_repo_dir),
            allow_reserved=True,
        )

        remote_repo_dir = golden_repos_dir / "test-repo"
        remote_repo_dir.mkdir()
        alias_manager.create_alias("test-repo-global", str(remote_repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",
            str(remote_repo_dir),
        )

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
        )

        submitted = []

        def capture_and_stop(alias_name):
            submitted.append(alias_name)
            if len(submitted) >= 2:
                scheduler._running = False

        with patch.object(
            scheduler, "_submit_refresh_job", side_effect=capture_and_stop
        ), patch.object(scheduler, "get_refresh_interval", return_value=0):
            scheduler._running = True
            scheduler._scheduler_loop()

        assert "cidx-meta-global" in submitted, (
            "C1 (Story #224): local:// repos must be submitted to _submit_refresh_job. "
            "The skip block was removed from _scheduler_loop()."
        )
        assert "test-repo-global" in submitted, (
            "Remote repos must still be submitted (regression guard)."
        )

    def test_execute_refresh_local_repo_uses_mtime_not_early_return(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        alias_manager,
    ):
        """
        C2 (Story #224): _execute_refresh() must NOT return early for local:// repos.

        Previously local repos returned {"success": True, "message": "Local repo, skipped"}
        immediately without reconciliation. After C2, local repos proceed through
        reconciliation and use _has_local_changes() for mtime-based detection.

        Expected:
        - _detect_existing_indexes() IS called for local repos
        - _reconcile_registry_with_filesystem() IS called for local repos
        - _has_local_changes() IS called for local repos
        - Result is NOT "Local repo, skipped"
        """
        local_repo_dir = golden_repos_dir / "cidx-meta"
        local_repo_dir.mkdir()
        alias_manager.create_alias("cidx-meta-global", str(local_repo_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            "local://cidx-meta",
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

        with patch.object(
            scheduler, "_detect_existing_indexes", return_value={}
        ) as mock_detect, patch.object(
            scheduler, "_reconcile_registry_with_filesystem"
        ) as mock_reconcile, patch.object(
            scheduler, "_has_local_changes", return_value=False
        ) as mock_mtime:
            result = scheduler._execute_refresh("cidx-meta-global")

            # Reconciliation must happen for local repos (not skipped early)
            assert mock_detect.call_count >= 1, (
                "C2 (Story #224): _detect_existing_indexes() must be called for local repos. "
                "Local repos no longer return early."
            )
            assert mock_reconcile.call_count >= 1, (
                "C2 (Story #224): _reconcile_registry_with_filesystem() must be called for local repos."
            )
            assert mock_mtime.call_count == 1, (
                "C2 (Story #224): _has_local_changes() must be called for mtime detection."
            )

        # Result must not be the old "Local repo, skipped" early return
        assert result["success"] is True
        assert result["message"] != "Local repo, skipped", (
            "C2 (Story #224): local repos must not return with old 'Local repo, skipped' message. "
            "They now use mtime detection and may return 'No changes detected' instead."
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
