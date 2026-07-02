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


def _make_scheduler(tmp_path, repo_url):
    """Factory: build a RefreshScheduler stub with heavy internals mocked out.

    Args:
        tmp_path: pytest tmp_path fixture value (unique per test).
        repo_url: Value to store in the registry stub's repo_info dict.
                  Use None for the legacy meta-repo marker or
                  "local://cidx-meta" for the post-migration production value.

    Returns:
        Configured RefreshScheduler instance ready for unit testing.
    """
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    # For local:// repos the scheduler reads source_path directly, so the
    # directory must exist and contain a .code-indexer sentinel.
    if repo_url and repo_url.startswith("local://"):
        cidx_meta_dir = golden_repos_dir / "cidx-meta"
        cidx_meta_dir.mkdir(parents=True)
        (cidx_meta_dir / ".code-indexer").mkdir()
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    registry = _RegistryStub({"repo_url": repo_url, "default_branch": "master"})
    sched = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=registry,
    )
    sched.alias_manager.read_alias = MagicMock(
        return_value=str(golden_repos_dir / ".versioned" / "cidx-meta" / "v_1")
    )
    sched.registry = registry
    sched._detect_existing_indexes = MagicMock(return_value={})
    sched._reconcile_registry_with_filesystem = MagicMock()
    sched._index_source = MagicMock()
    sched._create_snapshot = MagicMock(return_value=str(tmp_path / "snapshot"))
    sched.alias_manager.swap_alias = MagicMock()
    sched.is_write_locked = MagicMock(return_value=False)
    sched._reset_fetch_failures = MagicMock()
    if repo_url and repo_url.startswith("local://"):
        # Simulate no local file changes so early-return fires unless backup
        # gate correctly intercepts first.
        sched._has_local_changes = MagicMock(return_value=False)
    return sched


@pytest.fixture
def scheduler(tmp_path):
    """Scheduler with repo_url=None — legacy meta-directory marker."""
    return _make_scheduler(tmp_path, repo_url=None)


@pytest.fixture
def scheduler_local_url(tmp_path):
    """Scheduler with repo_url='local://cidx-meta' — production reality after migration.

    After migrate_legacy_cidx_meta() runs at server startup, cidx-meta is registered
    with repo_url='local://cidx-meta' (not None).  This makes is_meta_repo=False and
    is_local_repo=True in _execute_refresh, which was silently bypassing the backup gate.
    """
    return _make_scheduler(tmp_path, repo_url="local://cidx-meta")


def test_backup_enabled_runs_sync_when_repo_url_is_local(scheduler_local_url):
    """Regression: backup gate must fire even when repo_url='local://cidx-meta'.

    After migrate_legacy_cidx_meta(), cidx-meta-global has repo_url='local://cidx-meta',
    making is_local_repo=True and is_meta_repo=False.  Before the fix, _execute_refresh
    took the is_local_repo branch, hit _has_local_changes()=False, and returned early —
    completely bypassing the backup-aware block.  This test proves backup sync is called
    regardless of the local-change detection result.

    Story #926 AC8 regression guard.
    """
    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url="file:///tmp/remote.git"
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )
    sync_instance = MagicMock()
    sync_instance.sync.return_value = SimpleNamespace(skipped=False, sync_failure=None)

    with (
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=config_service,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
            return_value=sync_instance,
        ),
        patch("code_indexer.global_repos.refresh_scheduler.CidxMetaBackupBootstrap"),
        patch("code_indexer.global_repos.refresh_scheduler.MetaDirectoryUpdater"),
    ):
        scheduler_local_url._execute_refresh("cidx-meta-global")

    # The backup sync MUST be called even though _has_local_changes returned False
    sync_instance.sync.assert_called_once()


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
