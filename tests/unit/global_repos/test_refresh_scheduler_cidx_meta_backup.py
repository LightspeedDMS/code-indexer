"""Unit tests for Story #926 refresh scheduler backup-aware cidx-meta flow."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


class _RegistryStub:
    def __init__(self, repo_info):
        self._repo_info = repo_info

    def get_global_repo(self, alias_name):
        return self._repo_info

    def update_refresh_timestamp(self, alias_name):
        return None


@pytest.fixture
def scheduler(tmp_path):
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    registry = _RegistryStub({"repo_url": None, "default_branch": "master"})
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=registry,
    )
    scheduler.alias_manager.read_alias = MagicMock(
        return_value=str(golden_repos_dir / ".versioned" / "cidx-meta" / "v_1")
    )
    scheduler.registry = registry
    scheduler._detect_existing_indexes = MagicMock(return_value={})
    scheduler._reconcile_registry_with_filesystem = MagicMock()
    scheduler._index_source = MagicMock()
    scheduler._create_snapshot = MagicMock(return_value=str(tmp_path / "snapshot"))
    scheduler.alias_manager.swap_alias = MagicMock()
    scheduler.is_write_locked = MagicMock(return_value=False)
    scheduler._reset_fetch_failures = MagicMock()
    return scheduler


def test_backup_disabled_uses_existing_flow(scheduler):
    """# Story #926 AC7: when backup is disabled, cidx-meta continues using MetaDirectoryUpdater flow."""
    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(enabled=False, remote_url="")
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )
    with (
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=config_service,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.MetaDirectoryUpdater"
        ) as updater_cls,
    ):
        updater = updater_cls.return_value
        updater.has_changes.return_value = True

        scheduler._execute_refresh("cidx-meta-global")

    updater_cls.assert_called_once()
    updater.update.assert_called_once()


def test_backup_enabled_runs_sync_before_index(scheduler):
    """# Story #926 AC2: backup-aware cidx-meta flow runs sync before indexing."""
    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url="file:///tmp/remote.git"
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )
    call_order = []
    sync_instance = MagicMock()

    def _record_sync():
        call_order.append("sync")
        return SimpleNamespace(skipped=False, sync_failure=None)

    sync_instance.sync.side_effect = _record_sync

    def _record_index(*args, **kwargs):
        call_order.append("index")
        return None

    scheduler._index_source.side_effect = _record_index

    with (
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=config_service,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
            return_value=sync_instance,
        ),
    ):
        scheduler._execute_refresh("cidx-meta-global")

    assert call_order[:2] == ["sync", "index"]


def test_index_runs_even_after_push_failure(scheduler):
    """# Story #926 AC6: indexing still runs when sync reports a deferred push failure, then the job fails last."""
    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url="file:///tmp/remote.git"
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )
    sync_instance = MagicMock()
    sync_instance.sync.return_value = SimpleNamespace(
        skipped=False, sync_failure="push failed: boom"
    )

    with (
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=config_service,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
            return_value=sync_instance,
        ),
    ):
        with pytest.raises(
            RuntimeError,
            match="refresh complete, indexing succeeded, but backup push failed: boom",
        ):
            scheduler._execute_refresh("cidx-meta-global")

    scheduler._index_source.assert_called_once()


def test_index_runs_even_after_fetch_failure(scheduler):
    """# Story #926 AC6: indexing still runs when sync reports a deferred fetch failure, then the job fails last."""
    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url="file:///tmp/remote.git"
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )
    sync_instance = MagicMock()
    sync_instance.sync.return_value = SimpleNamespace(
        skipped=False, sync_failure="fetch failed: boom"
    )

    with (
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=config_service,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
            return_value=sync_instance,
        ),
    ):
        with pytest.raises(
            RuntimeError,
            match="refresh complete, indexing succeeded, but backup fetch failed: boom",
        ):
            scheduler._execute_refresh("cidx-meta-global")

    scheduler._index_source.assert_called_once()


def test_skipped_sync_returns_early_without_force(scheduler):
    """# Story #926 AC2: skipped sync returns a no-changes success without indexing when force_reset is false."""
    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url="file:///tmp/remote.git"
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )
    sync_instance = MagicMock()
    sync_instance.sync.return_value = SimpleNamespace(skipped=True, sync_failure=None)

    with (
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=config_service,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
            return_value=sync_instance,
        ),
    ):
        result = scheduler._execute_refresh("cidx-meta-global", force_reset=False)

    assert result["success"] is True
    assert result["message"] == "No changes detected"
    scheduler._index_source.assert_not_called()
