"""
Unit tests for RefreshScheduler cleanup guard (Story #236 + Bug #1084 Phase A4).

The cleanup gate decides whether the SUPERSEDED snapshot should be scheduled for
deletion after an alias swap. Two invariants:

- AC1 (Story #236): the master golden repo (golden-repos/{repo}) must NEVER be
  scheduled for cleanup — on first refresh current_target IS the master.
- AC3 (Story #236): old versioned snapshots MUST be scheduled for cleanup.

Bug #1084 Phase A4: the gate previously used a brittle ``".versioned" in target``
substring test that only recognized the LocalCloneBackend layout. It now uses the
canonical predicate (``snapshot_manager.is_versioned_snapshot``) and an explicit
master-path comparison, so cow-daemon canonical AND legacy snapshots are
recognized while the master is always preserved.
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


def _make_cow_snapshot_manager(mount_point):
    """Build a real VersionedSnapshotManager wired to a CowDaemonBackend so the
    gate's predicate recognizes cow canonical AND legacy shapes under the mount."""
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )
    from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
    from code_indexer.server.utils.config_manager import CowDaemonConfig

    backend = CowDaemonBackend(
        config=CowDaemonConfig(
            daemon_url="http://daemon:8081",
            api_key="k",
            mount_point=mount_point,
        )
    )
    return VersionedSnapshotManager(clone_backend=backend)


def _make_scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    snapshot_manager=None,
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
        snapshot_manager=snapshot_manager,
    )


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    """RefreshScheduler with a mock registry and NO snapshot_manager (local mode)."""
    return _make_scheduler(
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
    )


def _run_refresh(scheduler, golden_repos_dir, current_target, new_versioned_path):
    """Drive _execute_refresh with the given current_target through the swap."""
    alias_name = "my-repo-global"
    master_path = str(golden_repos_dir / "my-repo")
    (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

    scheduler.registry.get_global_repo.return_value = {
        "alias_name": alias_name,
        "repo_url": "git@github.com:org/my-repo.git",
    }

    with (
        patch.object(
            scheduler.alias_manager, "read_alias", return_value=current_target
        ),
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


# ---------------------------------------------------------------------------
# AC1: master preserved (Story #236)
# ---------------------------------------------------------------------------


class TestMasterPreserved:
    def test_master_path_local_not_scheduled(
        self, scheduler, golden_repos_dir, mock_cleanup_manager
    ):
        """First refresh: current_target IS the master — never schedule it."""
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_1000000")

        _run_refresh(scheduler, golden_repos_dir, master_path, new_versioned)

        mock_cleanup_manager.schedule_cleanup.assert_not_called()

    def test_master_path_preserved_even_with_cow_snapshot_manager(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
    ):
        """With a cow snapshot_manager wired, the master base clone (which is NOT
        under the mount in canonical/legacy snapshot shape) is still preserved."""
        sm = _make_cow_snapshot_manager(mount_point="/mnt/cow-storage")
        sched = _make_scheduler(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            snapshot_manager=sm,
        )
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned = "/mnt/cow-storage/.versioned/my-repo/v_2000000"

        _run_refresh(sched, golden_repos_dir, master_path, new_versioned)

        mock_cleanup_manager.schedule_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# AC3: superseded snapshots scheduled (Story #236 + Bug #1084)
# ---------------------------------------------------------------------------


class TestSupersededSnapshotScheduled:
    def test_local_canonical_snapshot_scheduled(
        self, scheduler, golden_repos_dir, mock_cleanup_manager
    ):
        old_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_1000000")
        new_versioned = str(golden_repos_dir / ".versioned" / "my-repo" / "v_2000000")

        _run_refresh(scheduler, golden_repos_dir, old_versioned, new_versioned)

        mock_cleanup_manager.schedule_cleanup.assert_called_once_with(old_versioned)

    def test_cow_canonical_snapshot_scheduled(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
    ):
        sm = _make_cow_snapshot_manager(mount_point="/mnt/cow-storage")
        sched = _make_scheduler(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            snapshot_manager=sm,
        )
        old_versioned = "/mnt/cow-storage/.versioned/my-repo/v_1700000000"
        new_versioned = "/mnt/cow-storage/.versioned/my-repo/v_1700009999"

        _run_refresh(sched, golden_repos_dir, old_versioned, new_versioned)

        mock_cleanup_manager.schedule_cleanup.assert_called_once_with(old_versioned)

    def test_cow_legacy_snapshot_scheduled(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
    ):
        """Bug #1084: legacy cow snapshot ({mount}/{ns}/v_*) is recognized and scheduled."""
        sm = _make_cow_snapshot_manager(mount_point="/mnt/cow-storage")
        sched = _make_scheduler(
            golden_repos_dir,
            mock_config_source,
            mock_query_tracker,
            mock_cleanup_manager,
            mock_registry,
            snapshot_manager=sm,
        )
        old_versioned = "/mnt/cow-storage/my-repo/v_1699999999"
        new_versioned = "/mnt/cow-storage/.versioned/my-repo/v_1700009999"

        _run_refresh(sched, golden_repos_dir, old_versioned, new_versioned)

        mock_cleanup_manager.schedule_cleanup.assert_called_once_with(old_versioned)


# ---------------------------------------------------------------------------
# Bug #1084: None alias graceful handling (no TypeError)
# ---------------------------------------------------------------------------


class TestNoneAliasGraceful:
    def test_none_current_target_no_crash_no_schedule(
        self, scheduler, golden_repos_dir, mock_cleanup_manager
    ):
        """A missing alias (read_alias -> None) must not raise; the refresh
        short-circuits before the gate, so cleanup is never scheduled."""
        alias_name = "my-repo-global"

        with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
            result = scheduler._execute_refresh(alias_name)

        # Refresh returns gracefully (alias-not-found short-circuit).
        assert result["success"] is True
        mock_cleanup_manager.schedule_cleanup.assert_not_called()
