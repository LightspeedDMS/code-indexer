"""
Tests for RefreshScheduler - timer-triggered refresh orchestration.

Tests AC1, AC2, AC3, AC6 Technical Requirements:
- Timer-triggered refresh at configured intervals
- Git pull and change detection
- New versioned index creation
- Atomic alias swap
- Query-aware cleanup scheduling
- Error handling and recovery
"""

from pathlib import Path
from typing import Dict
from unittest.mock import patch, MagicMock
import subprocess

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


class TestRefreshScheduler:
    """Test suite for RefreshScheduler component."""

    def test_scheduler_starts_and_stops(self, tmp_path):
        """
        Test that scheduler can be started and stopped cleanly.

        Basic lifecycle management
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        scheduler.start()
        assert scheduler.is_running()

        scheduler.stop()
        assert not scheduler.is_running()

    def test_scheduler_uses_configured_interval(self, tmp_path):
        """
        Test that scheduler uses the configured refresh interval.

        AC5: All repos use same interval
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        config_mgr.set_global_refresh_interval(300)  # 5 minutes

        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        # Verify interval is read from config
        assert scheduler.get_refresh_interval() == 300

    def test_refresh_repo_executes_git_pull(self, tmp_path):
        """
        Test that refresh_repo() executes git pull via updater.

        AC1: Git pull operation on golden repo source
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        # Create mock golden repo
        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        # Create alias and registry entry
        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        # Mock updater
        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False  # No changes
            mock_updater_cls.return_value = mock_updater

            scheduler.refresh_repo("test-repo-global")

            # Verify updater was called
            mock_updater.has_changes.assert_called_once()

    def test_refresh_skips_if_no_changes(self, tmp_path):
        """
        Test that refresh skips indexing if no git changes detected.

        AC1: Change detection before full reindex (skip if no changes)
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False  # No changes
            mock_updater_cls.return_value = mock_updater

            scheduler.refresh_repo("test-repo-global")

            # Verify update() was NOT called (skipped)
            mock_updater.update.assert_not_called()

    def test_refresh_executes_update_if_changes_detected(self, tmp_path):
        """
        Test that refresh executes git pull when changes detected.

        AC1: Git pull and indexing when changes exist
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True  # Changes detected
            mock_updater_cls.return_value = mock_updater

            with patch.object(scheduler, "_create_new_index") as mock_create_index:
                mock_create_index.return_value = str(tmp_path / "v_new")

                scheduler.refresh_repo("test-repo-global")

                # Verify update was called
                mock_updater.update.assert_called_once()

    def test_refresh_creates_versioned_index_directory(self, tmp_path):
        """
        Test that refresh creates new versioned index directory.

        AC1: New index directory with timestamp version
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            with patch.object(scheduler, "_create_new_index") as mock_create_index:
                new_index_path = str(tmp_path / "v_1234567890")
                mock_create_index.return_value = new_index_path

                scheduler.refresh_repo("test-repo-global")

                # Verify _create_new_index was called
                mock_create_index.assert_called_once()

    def test_refresh_swaps_alias_after_indexing(self, tmp_path):
        """
        Test that refresh swaps alias pointer after creating new index.

        AC2: Atomic alias swap after index creation
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        old_index = str(repo_dir / "v_old")

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", old_index)
        registry.register_global_repo(
            "test-repo", "test-repo-global", "https://github.com/test/repo", old_index
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            new_index = str(tmp_path / "v_new")
            with patch.object(scheduler, "_create_new_index") as mock_create_index:
                mock_create_index.return_value = new_index

                scheduler.refresh_repo("test-repo-global")

                # Verify alias was swapped
                current_target = alias_mgr.read_alias("test-repo-global")
                assert current_target == new_index

    def test_refresh_schedules_cleanup_of_old_index(self, tmp_path):
        """
        Test that refresh schedules cleanup of old index after swap.

        AC3: Old index scheduled for cleanup
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        old_index = str(repo_dir / "v_old")

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", old_index)
        registry.register_global_repo(
            "test-repo", "test-repo-global", "https://github.com/test/repo", old_index
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            new_index = str(tmp_path / "v_new")
            with patch.object(scheduler, "_create_new_index") as mock_create_index:
                mock_create_index.return_value = new_index

                scheduler.refresh_repo("test-repo-global")

                # Verify old index is in cleanup queue
                pending = cleanup_mgr.get_pending_cleanups()
                assert old_index in pending

    def test_refresh_handles_git_pull_failure(self, tmp_path, caplog):
        """
        Test that refresh handles git pull failure gracefully.

        AC6: Failed refresh handling - error logged, current index unchanged
        """
        import logging

        caplog.set_level(logging.ERROR)

        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        old_index = str(repo_dir / "v_old")

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", old_index)
        registry.register_global_repo(
            "test-repo", "test-repo-global", "https://github.com/test/repo", old_index
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.side_effect = RuntimeError("Network error")
            mock_updater_cls.return_value = mock_updater

            # Refresh should not raise exception
            scheduler.refresh_repo("test-repo-global")

            # Verify error was logged
            assert "Refresh failed" in caplog.text
            assert "test-repo-global" in caplog.text

            # Verify alias unchanged
            current_target = alias_mgr.read_alias("test-repo-global")
            assert current_target == old_index

    def test_scheduler_double_start_is_safe(self, tmp_path):
        """
        Test that calling start() twice is safe (idempotent).

        Error handling: Prevent duplicate threads
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        scheduler.start()
        scheduler.start()  # Should be no-op

        assert scheduler.is_running()

        scheduler.stop()

    def test_scheduler_double_stop_is_safe(self, tmp_path):
        """
        Test that calling stop() twice is safe (idempotent).
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        scheduler.start()
        scheduler.stop()
        scheduler.stop()  # Should be no-op

        assert not scheduler.is_running()

    def test_refresh_uses_meta_directory_updater_for_meta_repo(self, tmp_path):
        """
        Test that RefreshScheduler uses MetaDirectoryUpdater for meta-directory.

        CRITICAL: Meta-directory (repo_url=None) should use MetaDirectoryUpdater,
        not GitPullUpdater.

        This test will FAIL until RefreshScheduler is fixed to check repo_url.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        # Create meta-directory
        meta_dir = golden_repos_dir / "cidx-meta"
        meta_dir.mkdir()

        # Create alias and registry entry for meta-directory
        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("cidx-meta-global", str(meta_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            None,  # Special marker for meta-directory
            str(meta_dir),
            allow_reserved=True,
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        # Mock MetaDirectoryUpdater
        with patch(
            "code_indexer.global_repos.refresh_scheduler.MetaDirectoryUpdater"
        ) as mock_meta_updater_cls:
            mock_meta_updater = MagicMock()
            mock_meta_updater.has_changes.return_value = False
            mock_meta_updater_cls.return_value = mock_meta_updater

            # Mock GitPullUpdater to ensure it's NOT called
            with patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
            ) as mock_git_updater_cls:
                scheduler.refresh_repo("cidx-meta-global")

                # Verify MetaDirectoryUpdater was used
                # Check the call arguments (path and registry instance)
                assert mock_meta_updater_cls.call_count == 1
                call_args = mock_meta_updater_cls.call_args
                assert call_args[0][0] == str(meta_dir)  # First positional arg is path
                assert isinstance(call_args[0][1], GlobalRegistry)  # Second is registry
                mock_meta_updater.has_changes.assert_called_once()

                # Verify GitPullUpdater was NOT used
                mock_git_updater_cls.assert_not_called()

    def test_refresh_uses_git_pull_updater_for_normal_repos(self, tmp_path):
        """
        Test that RefreshScheduler uses GitPullUpdater for normal repos.

        Ensures that the meta-directory fix doesn't break normal repo refreshes.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",  # Normal repo has URL
            str(repo_dir),
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        # Mock GitPullUpdater
        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_git_updater_cls:
            mock_git_updater = MagicMock()
            mock_git_updater.has_changes.return_value = False
            mock_git_updater_cls.return_value = mock_git_updater

            # Mock MetaDirectoryUpdater to ensure it's NOT called
            with patch(
                "code_indexer.global_repos.refresh_scheduler.MetaDirectoryUpdater"
            ) as mock_meta_updater_cls:
                scheduler.refresh_repo("test-repo-global")

                # Verify GitPullUpdater was used
                mock_git_updater_cls.assert_called_once_with(str(repo_dir))
                mock_git_updater.has_changes.assert_called_once()

                # Verify MetaDirectoryUpdater was NOT used
                mock_meta_updater_cls.assert_not_called()


class TestRefreshSchedulerIndexReconciliation:
    """Test suite for Story #70 - Auto-Refresh Index Reconciliation."""

    def test_detect_existing_indexes_all_present(self, tmp_path):
        """
        AC1: Test _detect_existing_indexes() detects all index types.

        When all index directories exist on disk, should return True for all.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        # Create repository with all index types
        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()

        # Create semantic index
        semantic_dir = repo_dir / ".code-indexer" / "index" / "code-indexer"
        semantic_dir.mkdir(parents=True)

        # Create FTS index (production path: tantivy_index)
        fts_dir = repo_dir / ".code-indexer" / "tantivy_index"
        fts_dir.mkdir(parents=True)

        # Create temporal index (production path: index/code-indexer-temporal)
        temporal_dir = repo_dir / ".code-indexer" / "index" / "code-indexer-temporal"
        temporal_dir.mkdir(parents=True)

        # Create SCIP index (*.scip.db files)
        scip_dir = repo_dir / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        (scip_dir / "project.scip.db").touch()

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        detected = scheduler._detect_existing_indexes(repo_dir)

        assert detected == {
            "semantic": True,
            "fts": True,
            "temporal": True,
            "scip": True,
        }

    def test_detect_existing_indexes_none_present(self, tmp_path):
        """
        AC1: Test _detect_existing_indexes() when no indexes exist.

        When repository has no indexes, should return False for all.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        # Create repository with no indexes
        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        detected = scheduler._detect_existing_indexes(repo_dir)

        assert detected == {
            "semantic": False,
            "fts": False,
            "temporal": False,
            "scip": False,
        }

    def test_detect_existing_indexes_partial(self, tmp_path):
        """
        AC1: Test _detect_existing_indexes() with partial indexes.

        When only some indexes exist, should accurately detect which ones.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        # Create repository with only semantic and temporal indexes
        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()

        semantic_dir = repo_dir / ".code-indexer" / "index" / "code-indexer"
        semantic_dir.mkdir(parents=True)

        temporal_dir = repo_dir / ".code-indexer" / "index" / "code-indexer-temporal"
        temporal_dir.mkdir(parents=True)

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        detected = scheduler._detect_existing_indexes(repo_dir)

        assert detected == {
            "semantic": True,
            "fts": False,
            "temporal": True,
            "scip": False,
        }

    def test_has_scip_indexes_with_db_files(self, tmp_path):
        """
        AC1: Test _has_scip_indexes() detects .scip.db files.

        When SCIP directory contains .scip.db files, should return True.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()

        scip_dir = repo_dir / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        (scip_dir / "index.scip.db").touch()
        (scip_dir / "another.scip.db").touch()

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        assert scheduler._has_scip_indexes(repo_dir) is True

    def test_has_scip_indexes_no_db_files(self, tmp_path):
        """
        AC1: Test _has_scip_indexes() when no .scip.db files exist.

        When SCIP directory exists but has no .scip.db files, should return False.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()

        scip_dir = repo_dir / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        (scip_dir / "metadata.json").touch()  # Non-.scip.db file

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        assert scheduler._has_scip_indexes(repo_dir) is False

    def test_has_scip_indexes_directory_missing(self, tmp_path):
        """
        AC1: Test _has_scip_indexes() when SCIP directory doesn't exist.

        When SCIP directory is missing, should return False.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
        )

        assert scheduler._has_scip_indexes(repo_dir) is False

    def test_reconcile_registry_enables_temporal_when_found(self, tmp_path):
        """
        AC2: Test _reconcile_registry_with_filesystem() enables temporal when index exists.

        When temporal index exists on disk but registry says disabled, should enable it.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_temporal=False,  # Disabled in registry
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry to avoid SQLite backend
        )

        # Simulate temporal index found on disk
        detected = {
            "semantic": True,
            "fts": True,
            "temporal": True,  # Found on disk
            "scip": False,
        }

        scheduler._reconcile_registry_with_filesystem("test-repo-global", detected)

        # Verify registry updated
        repo_info = registry.get_global_repo("test-repo-global")
        assert repo_info["enable_temporal"] is True

    def test_reconcile_registry_disables_temporal_when_missing(self, tmp_path):
        """
        AC2: Test _reconcile_registry_with_filesystem() disables temporal when index missing.

        When temporal index missing from disk but registry says enabled, should disable it.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_temporal=True,  # Enabled in registry
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry to avoid SQLite backend
        )

        # Simulate temporal index missing from disk
        detected = {
            "semantic": True,
            "fts": True,
            "temporal": False,  # Missing from disk
            "scip": False,
        }

        scheduler._reconcile_registry_with_filesystem("test-repo-global", detected)

        # Verify registry updated
        repo_info = registry.get_global_repo("test-repo-global")
        assert repo_info["enable_temporal"] is False

    def test_reconcile_registry_enables_scip_when_found(self, tmp_path):
        """
        AC2/AC3: Test _reconcile_registry_with_filesystem() enables SCIP when index exists.

        When SCIP index exists on disk but registry says disabled, should enable it.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_scip=False,  # Disabled in registry
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry to avoid SQLite backend
        )

        # Simulate SCIP index found on disk
        detected = {
            "semantic": True,
            "fts": True,
            "temporal": False,
            "scip": True,  # Found on disk
        }

        scheduler._reconcile_registry_with_filesystem("test-repo-global", detected)

        # Verify registry updated
        repo_info = registry.get_global_repo("test-repo-global")
        assert repo_info["enable_scip"] is True

    def test_reconcile_registry_disables_scip_when_missing(self, tmp_path):
        """
        AC2/AC3: Test _reconcile_registry_with_filesystem() disables SCIP when index missing.

        When SCIP index missing from disk but registry says enabled, should disable it.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_scip=True,  # Enabled in registry
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry to avoid SQLite backend
        )

        # Simulate SCIP index missing from disk
        detected = {
            "semantic": True,
            "fts": True,
            "temporal": False,
            "scip": False,  # Missing from disk
        }

        scheduler._reconcile_registry_with_filesystem("test-repo-global", detected)

        # Verify registry updated
        repo_info = registry.get_global_repo("test-repo-global")
        assert repo_info["enable_scip"] is False

    def test_refresh_creates_scip_index_when_enabled(self, tmp_path) -> None:
        """AC4: _create_new_index should run 'cidx scip generate' when enable_scip=True."""
        # Setup: Golden repo with enable_scip=True
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_temporal=False,
            enable_scip=True,  # SCIP enabled
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry
        )

        # Mock subprocess.run to avoid actual cidx commands
        with patch("subprocess.run") as mock_run:
            def mock_subprocess(*args, **kwargs):
                # Create .code-indexer/index directory when cidx index is called
                # This satisfies the validation check in _create_new_index
                command = args[0] if args else kwargs.get("args", [])
                cwd = kwargs.get("cwd")
                if cwd and "cidx" in command and "index" in command and "scip" not in command:
                    # This is "cidx index" command - create the index directory
                    index_dir = Path(cwd) / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = mock_subprocess

            # Execute: Create new index
            index_path = scheduler._create_new_index(
                alias_name="test-repo-global", source_path=str(repo_dir)
            )

            # Verify: cidx scip generate was called
            scip_calls = [
                call
                for call in mock_run.call_args_list
                if len(call[0]) > 0 and isinstance(call[0][0], list) and len(call[0][0]) > 0 and call[0][0][0] == "cidx" and "scip" in call[0][0]
            ]
            assert len(scip_calls) > 0, f"cidx scip generate should be called when enable_scip=True. All calls: {[call[0][0] for call in mock_run.call_args_list if len(call[0]) > 0]}"

            # Verify the scip command was: ["cidx", "scip", "generate"]
            scip_command = scip_calls[0][0][0]
            assert scip_command == ["cidx", "scip", "generate"], f"Expected ['cidx', 'scip', 'generate'], got {scip_command}"

    def test_refresh_skips_scip_index_when_disabled(self, tmp_path) -> None:
        """AC4: _create_new_index should NOT run SCIP generation when enable_scip=False."""
        # Setup: Golden repo with enable_scip=False
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_temporal=False,
            enable_scip=False,  # SCIP disabled
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry
        )

        # Mock subprocess.run to avoid actual cidx commands
        with patch("subprocess.run") as mock_run:
            def mock_subprocess(*args, **kwargs):
                # Create .code-indexer/index directory when cidx index is called
                # This satisfies the validation check in _create_new_index
                command = args[0] if args else kwargs.get("args", [])
                cwd = kwargs.get("cwd")
                if cwd and "cidx" in command and "index" in command and "scip" not in command:
                    # This is "cidx index" command - create the index directory
                    index_dir = Path(cwd) / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = mock_subprocess

            # Execute: Create new index
            index_path = scheduler._create_new_index(
                alias_name="test-repo-global", source_path=str(repo_dir)
            )

            # Verify: cidx scip generate was NOT called
            scip_calls = [
                call
                for call in mock_run.call_args_list
                if len(call[0]) > 0 and isinstance(call[0][0], list) and len(call[0][0]) > 0 and call[0][0][0] == "cidx" and "scip" in call[0][0]
            ]
            assert len(scip_calls) == 0, "cidx scip generate should NOT be called when enable_scip=False"

    def test_refresh_uses_configured_scip_timeout(self, tmp_path) -> None:
        """AC4: _create_new_index should use cidx_scip_generate_timeout from ServerResourceConfig."""
        # Setup: Golden repo with enable_scip=True and custom timeout
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_temporal=False,
            enable_scip=True,  # SCIP enabled
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        # Create custom resource config with specific SCIP timeout
        from code_indexer.server.utils.config_manager import ServerResourceConfig
        custom_resource_config = ServerResourceConfig()
        custom_resource_config.cidx_scip_generate_timeout = 3000  # 50 minutes

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry
            resource_config=custom_resource_config,  # Inject custom config
        )

        # Mock subprocess.run to capture timeout parameter
        captured_timeouts = []

        with patch("subprocess.run") as mock_run:
            def mock_subprocess(*args, **kwargs):
                # Capture timeout for SCIP commands
                command = args[0] if args else kwargs.get("args", [])
                if isinstance(command, list) and "scip" in command:
                    captured_timeouts.append(kwargs.get("timeout"))
                # Create .code-indexer/index directory when cidx index is called
                cwd = kwargs.get("cwd")
                if cwd and "cidx" in command and "index" in command and "scip" not in command:
                    index_dir = Path(cwd) / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = mock_subprocess

            # Execute: Create new index
            index_path = scheduler._create_new_index(
                alias_name="test-repo-global", source_path=str(repo_dir)
            )

            # Verify: SCIP command used custom timeout
            assert len(captured_timeouts) == 1, "Should have captured one SCIP timeout"
            assert captured_timeouts[0] == 3000, f"Expected timeout=3000, got {captured_timeouts[0]}"

    def test_refresh_scip_failure_raises_runtime_error(self, tmp_path) -> None:
        """AC5: SCIP generation failures should raise RuntimeError and fail the refresh."""
        # Setup: Golden repo with enable_scip=True but invalid structure for SCIP
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo-invalid"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        # Create an invalid Python file that will cause SCIP indexer to fail
        invalid_file = repo_dir / "invalid_syntax.py"
        invalid_file.write_text("def broken( syntax error here")

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-invalid-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo-invalid",
            "test-repo-invalid-global",
            "https://github.com/test/repo-invalid",
            str(repo_dir),
            enable_temporal=False,
            enable_scip=True,  # SCIP enabled - will fail on invalid syntax
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry
        )

        # Mock subprocess.run to simulate SCIP failure
        with patch("subprocess.run") as mock_run:
            def mock_subprocess(*args, **kwargs):
                # First calls succeed (cp, git, cidx index)
                # SCIP call fails
                command = args[0] if args else kwargs.get("args", [])
                if "scip" in command:
                    raise subprocess.CalledProcessError(1, command, stderr="SCIP indexer failed: invalid syntax")
                # Other calls succeed
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = mock_subprocess

            # Execute & Verify: Should raise RuntimeError when SCIP generation fails
            with pytest.raises(RuntimeError, match="SCIP indexing failed"):
                scheduler._create_new_index(
                    alias_name="test-repo-invalid-global",
                    source_path=str(repo_dir),
                )

    def test_refresh_reconciles_at_start(self, tmp_path, monkeypatch) -> None:
        """AC6: _execute_refresh should call _reconcile_registry_with_filesystem at START."""
        # Setup: Golden repo with temporal enabled in registry but missing on disk
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_temporal=True,  # Enabled in registry
            enable_scip=False,
        )

        # Create .code-indexer directory without temporal (simulates missing temporal on disk)
        code_indexer_dir = repo_dir / ".code-indexer"
        code_indexer_dir.mkdir(parents=True)
        index_dir = code_indexer_dir / "index"
        index_dir.mkdir()
        # Create semantic and fts directories but NO temporal
        (code_indexer_dir / "tantivy_index").mkdir()

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry
        )

        # Track reconciliation calls
        reconciliation_calls = []

        original_reconcile = scheduler._reconcile_registry_with_filesystem

        def mock_reconcile(alias_name: str, detected: Dict[str, bool]) -> None:
            reconciliation_calls.append(("reconcile", alias_name, detected.copy()))
            original_reconcile(alias_name, detected)

        monkeypatch.setattr(
            scheduler, "_reconcile_registry_with_filesystem", mock_reconcile
        )

        # Mock GitPullUpdater to avoid actual git operations
        with patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False  # No changes = skip refresh
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            # Execute: Run refresh
            scheduler._execute_refresh("test-repo-global")

            # Verify: Reconciliation called at START and detected temporal=False
            assert len(reconciliation_calls) >= 1, "Should call reconciliation at START"
            first_call = reconciliation_calls[0]
            assert first_call[0] == "reconcile"
            assert first_call[1] == "test-repo-global"
            assert (
                first_call[2]["temporal"] is False
            ), "Should detect temporal missing at START"

            # Verify: Registry updated to match filesystem (temporal disabled)
            repo_info = registry.get_global_repo("test-repo-global")
            assert (
                repo_info["enable_temporal"] is False
            ), "Registry should be updated at START"

    def test_refresh_reconciles_at_end(self, tmp_path, monkeypatch) -> None:
        """AC6: _execute_refresh should call _reconcile_registry_with_filesystem at END."""
        # Setup: Golden repo with temporal disabled in registry
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        alias_mgr = AliasManager(str(golden_repos_dir / "aliases"))
        registry = GlobalRegistry(str(golden_repos_dir))

        alias_mgr.create_alias("test-repo-global", str(repo_dir))
        registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "https://github.com/test/repo",
            str(repo_dir),
            enable_temporal=False,  # Disabled in registry
            enable_scip=False,
        )

        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        cleanup_mgr = CleanupManager(tracker)

        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=cleanup_mgr,
            registry=registry,  # Inject registry
        )

        # Track reconciliation calls
        reconciliation_calls = []

        original_reconcile = scheduler._reconcile_registry_with_filesystem

        def mock_reconcile(alias_name: str, detected: Dict[str, bool]) -> None:
            reconciliation_calls.append(("reconcile", alias_name, detected.copy()))
            original_reconcile(alias_name, detected)

        monkeypatch.setattr(
            scheduler, "_reconcile_registry_with_filesystem", mock_reconcile
        )

        # Create a new index path with temporal directory to simulate refresh creating it
        new_index_path = tmp_path / ".versioned" / "test-repo" / "v_12345"
        new_index_path.mkdir(parents=True)
        new_code_indexer = new_index_path / ".code-indexer"
        new_code_indexer.mkdir()
        new_index_dir = new_code_indexer / "index"
        new_index_dir.mkdir()
        # Create temporal directory in new index to simulate it was created during refresh
        temporal_dir = new_index_dir / "code-indexer-temporal"
        temporal_dir.mkdir()

        # Mock GitPullUpdater and _create_new_index
        with patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True  # Has changes = perform refresh
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            # Mock _create_new_index to return path with temporal
            with patch.object(scheduler, "_create_new_index", return_value=str(new_index_path)):
                # Execute: Run refresh (will call reconcile at START and END)
                scheduler._execute_refresh("test-repo-global")

                # Verify: Reconciliation called at START and END
                assert len(reconciliation_calls) >= 2, "Should call reconciliation at START+END"

                # Verify START call detected no temporal (from repo_dir)
                first_call = reconciliation_calls[0]
                assert first_call[1] == "test-repo-global"

                # Verify END call detected temporal (from new_index_path)
                last_call = reconciliation_calls[-1]
                assert last_call[0] == "reconcile"
                assert last_call[1] == "test-repo-global"
                assert (
                    last_call[2]["temporal"] is True
                ), "Should detect temporal present at END (from new index)"

                # Verify: Registry updated to match filesystem (temporal enabled)
                repo_info = registry.get_global_repo("test-repo-global")
                assert (
                    repo_info["enable_temporal"] is True
                ), "Registry should be updated at END"
