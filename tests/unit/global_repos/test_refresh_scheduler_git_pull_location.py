"""
Unit tests for RefreshScheduler git pull location fix (Story #236).

Tests the git pull location fix:
- AC2: GitPullUpdater must always be called with the master golden repo path,
  never with a versioned snapshot path.
- AC2 (snapshot source): _create_snapshot must receive master path as source,
  not the old versioned path.

The bug: after first refresh, alias points to .versioned/ path.
_execute_refresh() used current_target (versioned) as golden_repo_path,
so git pull happened in the versioned snapshot instead of master.

The fix: always derive master_path = golden_repos_dir / repo_name and
pass that to GitPullUpdater, regardless of what current_target points to.
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
# AC2: Git pull location and snapshot source
# ---------------------------------------------------------------------------


class TestGitPullLocation:
    """
    AC2: GitPullUpdater must always be called with the master golden repo path,
    never with a versioned snapshot path.
    """

    def test_git_pull_uses_master_path_when_alias_points_to_versioned(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC2: When alias currently points to a versioned snapshot,
        GitPullUpdater must still be called with the master path.
        """
        alias_name = "my-repo-global"
        old_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
        )
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        # Create the master directory so the fix can find it
        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        captured_git_pull_paths = []

        def capture_git_pull(path):
            captured_git_pull_paths.append(path)
            mock_updater = Mock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = master_path
            return mock_updater

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=old_versioned_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                side_effect=capture_git_pull,
            ),
        ):
            scheduler._execute_refresh(alias_name)

        # GitPullUpdater must have been called with master path
        assert len(captured_git_pull_paths) == 1, (
            f"Expected 1 GitPullUpdater call, got {len(captured_git_pull_paths)}"
        )
        assert captured_git_pull_paths[0] == master_path, (
            f"GitPullUpdater called with '{captured_git_pull_paths[0]}' "
            f"instead of master path '{master_path}'"
        )
        # Must NOT be called with old versioned path
        assert captured_git_pull_paths[0] != old_versioned_path, (
            f"GitPullUpdater must not be called with versioned path: {old_versioned_path}"
        )

    def test_git_pull_uses_master_path_on_first_refresh(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC2 (first refresh): When alias points to master (first refresh),
        GitPullUpdater must be called with master path.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        captured_git_pull_paths = []

        def capture_git_pull(path):
            captured_git_pull_paths.append(path)
            mock_updater = Mock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = master_path
            return mock_updater

        with (
            patch.object(scheduler.alias_manager, "read_alias", return_value=master_path),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                side_effect=capture_git_pull,
            ),
        ):
            scheduler._execute_refresh(alias_name)

        assert len(captured_git_pull_paths) == 1
        assert captured_git_pull_paths[0] == master_path

    def test_snapshot_created_from_master_not_from_versioned(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        AC2 (snapshot source): _create_snapshot must receive master path as source_path,
        not the old versioned snapshot path.
        This ensures snapshots are always from master, not from previous snapshots.
        """
        alias_name = "my-repo-global"
        old_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_1000000"
        )
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        captured_snapshot_sources = []

        def capture_snapshot(alias_name, source_path):
            captured_snapshot_sources.append(source_path)
            return new_versioned_path

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=old_versioned_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", side_effect=capture_snapshot),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
            ) as mock_git_updater_cls,
        ):
            mock_updater = Mock()
            mock_updater.has_changes.return_value = True
            mock_updater.get_source_path.return_value = master_path
            mock_git_updater_cls.return_value = mock_updater

            scheduler._execute_refresh(alias_name)

        assert len(captured_snapshot_sources) == 1, (
            f"Expected 1 _create_snapshot call, got {len(captured_snapshot_sources)}"
        )
        assert captured_snapshot_sources[0] == master_path, (
            f"_create_snapshot called with '{captured_snapshot_sources[0]}' "
            f"instead of master path '{master_path}'"
        )
        assert captured_snapshot_sources[0] != old_versioned_path, (
            f"_create_snapshot must not receive old versioned path: {old_versioned_path}"
        )
