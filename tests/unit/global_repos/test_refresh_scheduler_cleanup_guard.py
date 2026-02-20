"""
Unit tests for RefreshScheduler cleanup guard (Story #236).

Tests the cleanup guard fix:
- AC1: Master golden repo must NEVER be scheduled for cleanup
- AC3: Old versioned snapshots MUST still be scheduled for cleanup

The bug: _execute_refresh() called cleanup_manager.schedule_cleanup(current_target)
unconditionally. On first refresh, current_target IS the master golden repo,
so the master was deleted permanently.

The fix: Only schedule cleanup when current_target contains '.versioned'.
"""

import pytest
from unittest.mock import Mock, patch

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a temporary golden-repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "my-repo-global",
        "repo_url": "git@github.com:org/my-repo.git",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    """Create RefreshScheduler with a mock registry."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# AC1 + AC3: Cleanup guard
# ---------------------------------------------------------------------------


class TestCleanupGuard:
    """
    AC1: cleanup_manager.schedule_cleanup must NOT be called for master paths.
    AC3: cleanup_manager.schedule_cleanup MUST be called for .versioned/ paths.
    """

    def test_master_path_not_scheduled_for_cleanup_on_first_refresh(
        self, scheduler, golden_repos_dir, mock_cleanup_manager, mock_registry
    ):
        """
        AC1: On first refresh, current_target IS the master golden repo.
        cleanup_manager.schedule_cleanup must NOT be called with the master path.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
        )

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        with (
            patch.object(scheduler.alias_manager, "read_alias", return_value=master_path),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
            ) as mock_git_updater_cls,
        ):
            mock_updater = Mock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = master_path
            mock_git_updater_cls.return_value = mock_updater

            scheduler._execute_refresh(alias_name)

        # Master path must NOT be scheduled for cleanup
        for call_args in mock_cleanup_manager.schedule_cleanup.call_args_list:
            path_arg = call_args[0][0]
            assert path_arg != master_path, (
                f"Master golden repo was scheduled for cleanup: {path_arg}"
            )

    def test_cleanup_not_called_at_all_when_master_is_current_target(
        self, scheduler, golden_repos_dir, mock_cleanup_manager, mock_registry
    ):
        """
        AC1 (strict): When current_target does not contain '.versioned',
        schedule_cleanup must not be called at all.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_9999999"
        )

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        with (
            patch.object(scheduler.alias_manager, "read_alias", return_value=master_path),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
            ) as mock_git_updater_cls,
        ):
            mock_updater = Mock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = master_path
            mock_git_updater_cls.return_value = mock_updater

            scheduler._execute_refresh(alias_name)

        # schedule_cleanup must NOT be called at all when current_target is master
        mock_cleanup_manager.schedule_cleanup.assert_not_called()

    def test_versioned_path_is_scheduled_for_cleanup(
        self, scheduler, golden_repos_dir, mock_cleanup_manager, mock_registry
    ):
        """
        AC3: When current_target is a versioned snapshot (.versioned/ path),
        cleanup_manager.schedule_cleanup MUST be called with that path.
        """
        alias_name = "my-repo-global"
        old_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
        )
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )
        master_path = str(golden_repos_dir / "my-repo")

        # Create master directory so fix-up can find it
        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=old_versioned_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(
                scheduler, "_create_snapshot", return_value=new_versioned_path
            ),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
            ) as mock_git_updater_cls,
        ):
            mock_updater = Mock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = master_path
            mock_git_updater_cls.return_value = mock_updater

            scheduler._execute_refresh(alias_name)

        # Versioned path MUST be scheduled for cleanup
        mock_cleanup_manager.schedule_cleanup.assert_called_once_with(old_versioned_path)
