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
            registry=registry,
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
            registry=registry,
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
            registry=registry,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True  # Changes detected
            mock_updater_cls.return_value = mock_updater

            def fake_subprocess_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and cmd[0] == "cp":
                    dest = Path(cmd[-1])
                    index_dir = dest / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("subprocess.run", side_effect=fake_subprocess_run),
                patch(
                    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
                ),
            ):
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
            registry=registry,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            def fake_subprocess_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and cmd[0] == "cp":
                    dest = Path(cmd[-1])
                    index_dir = dest / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("subprocess.run", side_effect=fake_subprocess_run),
                patch(
                    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
                ),
            ):
                scheduler.refresh_repo("test-repo-global")

                # Verify a timestamped versioned child directory was created with index
                versioned_base = scheduler.golden_repos_dir / ".versioned" / "test-repo"
                assert versioned_base.exists(), (
                    "Versioned base directory should have been created"
                )
                version_dirs = [
                    d
                    for d in versioned_base.iterdir()
                    if d.is_dir() and d.name.startswith("v_")
                ]
                assert len(version_dirs) == 1, (
                    "Exactly one versioned snapshot directory should have been created"
                )
                assert (version_dirs[0] / ".code-indexer" / "index").exists(), (
                    "Versioned snapshot should contain .code-indexer/index"
                )

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
            registry=registry,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            def fake_subprocess_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and cmd[0] == "cp":
                    dest = Path(cmd[-1])
                    index_dir = dest / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("subprocess.run", side_effect=fake_subprocess_run),
                patch(
                    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
                ),
            ):
                scheduler.refresh_repo("test-repo-global")

                # Verify alias was swapped to a new versioned path
                current_target = alias_mgr.read_alias("test-repo-global")
                assert current_target != old_index, (
                    "Alias should point to new index after swap"
                )
                assert "v_" in current_target, (
                    "New alias target should be a versioned path"
                )

    def test_refresh_schedules_cleanup_of_old_index(self, tmp_path):
        """
        Test that refresh schedules cleanup of old index after swap.

        AC3: Old index scheduled for cleanup.
        The scheduler only schedules cleanup for versioned snapshots (paths containing
        ".versioned"), never for the master golden repo.
        """
        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)

        repo_dir = golden_repos_dir / "test-repo"
        repo_dir.mkdir()
        # Use a .versioned path so the scheduler's cleanup guard allows scheduling it
        old_index = str(
            golden_repos_dir / ".versioned" / "test-repo" / "v_old_snapshot"
        )

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
            registry=registry,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            def fake_subprocess_run(cmd, *args, **kwargs):
                if isinstance(cmd, list) and cmd[0] == "cp":
                    dest = Path(cmd[-1])
                    index_dir = dest / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("subprocess.run", side_effect=fake_subprocess_run),
                patch(
                    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
                ),
            ):
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
            registry=registry,
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.side_effect = RuntimeError("Network error")
            mock_updater_cls.return_value = mock_updater

            # Refresh raises RuntimeError (Bug #84: BackgroundJobManager needs exceptions)
            with pytest.raises(RuntimeError, match="Refresh failed"):
                scheduler.refresh_repo("test-repo-global")

            # Verify error was logged
            assert "Refresh failed" in caplog.text
            assert "test-repo-global" in caplog.text

            # Verify alias unchanged (failure left old index in place)
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
            registry=registry,
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
            registry=registry,
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

        # Mock subprocess.run (used by SCIP and _create_snapshot) and
        # run_with_popen_progress (used by semantic/temporal indexing steps).
        with (
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
            ),
        ):

            def mock_subprocess(*args, **kwargs):
                # Create .code-indexer/index directory when cp is called (CoW clone)
                # This satisfies the validation check in _create_snapshot
                command = args[0] if args else kwargs.get("args", [])
                if isinstance(command, list) and command and command[0] == "cp":
                    dest = Path(command[-1])
                    index_dir = dest / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = mock_subprocess

            # Execute: Create new index
            _index_path = scheduler._create_new_index(
                alias_name="test-repo-global", source_path=str(repo_dir)
            )

            # Verify: cidx scip generate was called via subprocess.run
            scip_calls = [
                c
                for c in mock_run.call_args_list
                if len(c[0]) > 0
                and isinstance(c[0][0], list)
                and len(c[0][0]) >= 3
                and c[0][0][0] == "cidx"
                and "scip" in c[0][0]
            ]
            assert len(scip_calls) > 0, (
                f"cidx scip generate should be called when enable_scip=True. "
                f"All subprocess.run calls: {[c[0][0] for c in mock_run.call_args_list if len(c[0]) > 0]}"
            )

            # Verify the scip command was: ["cidx", "scip", "generate"]
            scip_command = scip_calls[0][0][0]
            assert scip_command == [
                "cidx",
                "scip",
                "generate",
            ], f"Expected ['cidx', 'scip', 'generate'], got {scip_command}"

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

        # Mock subprocess.run (used by SCIP and _create_snapshot) and
        # run_with_popen_progress (used by semantic/temporal indexing steps).
        with (
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
            ),
        ):

            def mock_subprocess(*args, **kwargs):
                command = args[0] if args else kwargs.get("args", [])
                if isinstance(command, list) and command and command[0] == "cp":
                    dest = Path(command[-1])
                    index_dir = dest / ".code-indexer" / "index"
                    index_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = mock_subprocess

            # Execute: Create new index
            _index_path = scheduler._create_new_index(
                alias_name="test-repo-global", source_path=str(repo_dir)
            )

            # Verify: cidx scip generate was NOT called via subprocess.run
            scip_calls = [
                c
                for c in mock_run.call_args_list
                if len(c[0]) > 0
                and isinstance(c[0][0], list)
                and len(c[0][0]) >= 3
                and c[0][0][0] == "cidx"
                and "scip" in c[0][0]
            ]
            assert len(scip_calls) == 0, (
                "cidx scip generate should NOT be called when enable_scip=False"
            )

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

        # Mock run_with_popen_progress (semantic step) to succeed, and
        # subprocess.run to fail on SCIP command (simulating SCIP indexer error).
        with (
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
            ),
        ):

            def mock_subprocess(*args, **kwargs):
                command = args[0] if args else kwargs.get("args", [])
                if isinstance(command, list) and "scip" in command:
                    raise subprocess.CalledProcessError(
                        1, command, stderr="SCIP indexer failed: invalid syntax"
                    )
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = mock_subprocess

            # Execute & Verify: Should raise RuntimeError when SCIP generation fails
            with pytest.raises(RuntimeError, match="SCIP indexing on source failed"):
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
        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
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
            assert first_call[2]["temporal"] is False, (
                "Should detect temporal missing at START"
            )

            # Verify: Registry updated to match filesystem (temporal disabled)
            repo_info = registry.get_global_repo("test-repo-global")
            assert repo_info["enable_temporal"] is False, (
                "Registry should be updated at START"
            )

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

        # Mock GitPullUpdater (external git collaborator) and subprocess + popen runners
        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = (
                True  # Has changes = perform refresh
            )
            mock_updater.get_source_path.return_value = str(repo_dir)
            mock_updater_cls.return_value = mock_updater

            def fake_subprocess_run(cmd, *args, **kwargs):
                # When cp clones the source to a versioned snapshot, create the
                # .code-indexer/index and temporal dirs in the dest so END
                # reconciliation detects temporal=True.
                if isinstance(cmd, list) and cmd and cmd[0] == "cp":
                    dest = Path(cmd[-1])
                    temporal_dir = (
                        dest / ".code-indexer" / "index" / "code-indexer-temporal"
                    )
                    temporal_dir.mkdir(parents=True, exist_ok=True)
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch("subprocess.run", side_effect=fake_subprocess_run),
                patch(
                    "code_indexer.services.progress_subprocess_runner.run_with_popen_progress"
                ),
            ):
                # Execute: Run refresh (will call reconcile at START and END)
                scheduler._execute_refresh("test-repo-global")

                # Verify: Reconciliation called at START and END
                assert len(reconciliation_calls) >= 2, (
                    "Should call reconciliation at START+END"
                )

                # Verify START call used test-repo-global alias
                first_call = reconciliation_calls[0]
                assert first_call[1] == "test-repo-global"

                # Verify END call detected temporal (from snapshot with temporal dir)
                last_call = reconciliation_calls[-1]
                assert last_call[0] == "reconcile"
                assert last_call[1] == "test-repo-global"
                assert last_call[2]["temporal"] is True, (
                    "Should detect temporal present at END (from new index snapshot)"
                )

                # Verify: Registry updated to match filesystem (temporal enabled)
                repo_info = registry.get_global_repo("test-repo-global")
                assert repo_info["enable_temporal"] is True, (
                    "Registry should be updated at END"
                )

    @pytest.mark.slow
    def test_scheduler_survives_exception_before_refresh_interval_assigned(
        self, tmp_path
    ):
        """Regression: UnboundLocalError when exception fires before refresh_interval
        is assigned inside the while-loop try block (line 812). Fix: initialize
        refresh_interval = DEFAULT_REFRESH_INTERVAL before the loop. Proved by
        asserting the loop reaches a second iteration after the first exception.

        Marked slow: must wait for MAX_POLL_SECONDS (30s) between iterations.
        """
        import threading
        from unittest.mock import patch

        # After the first exception the outer except falls through to
        # _calculate_poll_interval(DEFAULT_REFRESH_INTERVAL=3600) which is clamped
        # to MAX_POLL_SECONDS=30s.  The second iteration therefore only happens after
        # a 30s sleep.  Timeout must exceed that to observe the second call.
        FIRST_CALL_TIMEOUT_SECONDS = 5.0
        SECOND_CALL_TIMEOUT_SECONDS = 35.0

        golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
        golden_repos_dir.mkdir(parents=True)
        config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        tracker = QueryTracker()
        registry = GlobalRegistry(str(golden_repos_dir))
        scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=tracker,
            cleanup_manager=CleanupManager(tracker),
            registry=registry,
        )

        first_call_raised = threading.Event()
        second_call_reached = threading.Event()
        call_count = [0]
        original_list = registry.list_global_repos

        def controlled_list():
            call_count[0] += 1
            if call_count[0] == 1:
                first_call_raised.set()
                raise RuntimeError("Simulated registry failure on first call")
            second_call_reached.set()
            return original_list()

        with patch.object(registry, "list_global_repos", controlled_list):
            scheduler.start()
            try:
                assert first_call_raised.wait(timeout=FIRST_CALL_TIMEOUT_SECONDS), (
                    "list_global_repos must be called within 5s"
                )
                reached = second_call_reached.wait(timeout=SECOND_CALL_TIMEOUT_SECONDS)
            finally:
                scheduler.stop()

        assert reached, (
            "Scheduler must survive the first exception and reach a second iteration; "
            "UnboundLocalError would crash the thread and prevent the second call"
        )
