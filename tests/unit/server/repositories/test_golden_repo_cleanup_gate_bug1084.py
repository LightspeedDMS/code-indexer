"""Bug #1084 Phase A4: GoldenRepoManager cleanup gates use the canonical predicate.

Covers the change-branch gate (`_cb_swap_alias`, was golden_repo_manager.py:2631)
and the shared `_is_versioned_snapshot` helper that both that gate and the
add-index gate (was :3228, now None-guarded) rely on. Before this fix both gates
used a brittle ``".versioned" in current_target`` substring test that leaked
cow-daemon snapshots (no ``.versioned`` in the legacy mount layout).
"""

from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


def _make_manager(tmp_path, snapshot_manager=None):
    mgr = GoldenRepoManager(data_dir=str(tmp_path))
    mgr._snapshot_manager = snapshot_manager
    return mgr


def _make_cow_snapshot_manager(mount_point):
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )
    from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
    from code_indexer.server.utils.config_manager import CowDaemonConfig

    backend = CowDaemonBackend(
        config=CowDaemonConfig(
            daemon_url="http://daemon:8081", api_key="k", mount_point=mount_point
        )
    )
    return VersionedSnapshotManager(clone_backend=backend)


class TestIsVersionedSnapshotHelper:
    def test_local_canonical_true(self, tmp_path):
        mgr = _make_manager(tmp_path)
        path = str(tmp_path / "golden-repos" / ".versioned" / "r" / "v_1700000000")
        assert mgr._is_versioned_snapshot(path) is True

    def test_master_path_false(self, tmp_path):
        mgr = _make_manager(tmp_path)
        master = str(tmp_path / "golden-repos" / "r")
        assert mgr._is_versioned_snapshot(master) is False

    def test_none_false_no_typeerror(self, tmp_path):
        """Bug #1084: :3228 add-index gate previously raised TypeError on None."""
        mgr = _make_manager(tmp_path)
        assert mgr._is_versioned_snapshot(None) is False

    def test_cow_canonical_true_with_snapshot_manager(self, tmp_path):
        sm = _make_cow_snapshot_manager("/mnt/cow-storage")
        mgr = _make_manager(tmp_path, snapshot_manager=sm)
        assert (
            mgr._is_versioned_snapshot("/mnt/cow-storage/.versioned/r/v_1700000000")
            is True
        )

    def test_cow_legacy_true_with_snapshot_manager(self, tmp_path):
        sm = _make_cow_snapshot_manager("/mnt/cow-storage")
        mgr = _make_manager(tmp_path, snapshot_manager=sm)
        assert mgr._is_versioned_snapshot("/mnt/cow-storage/r/v_1699999999") is True


class TestChangeBranchSwapGate:
    """`_cb_swap_alias` schedules cleanup only for snapshots, never the master."""

    @pytest.fixture
    def stub_cleanup_manager(self, monkeypatch):
        """Wire a stub lifecycle/refresh_scheduler/cleanup_manager into app.state."""
        from code_indexer.server import app as app_module

        cm = MagicMock()
        scheduler = MagicMock()
        scheduler.cleanup_manager = cm
        lifecycle = MagicMock()
        lifecycle.refresh_scheduler = scheduler
        monkeypatch.setattr(
            app_module.app.state, "global_lifecycle_manager", lifecycle, raising=False
        )
        return cm

    def _swap(self, mgr, alias, old_target, new_target):
        # Pre-seed the alias so read_alias returns old_target, create_alias overwrites.
        import os
        from code_indexer.global_repos.alias_manager import AliasManager

        aliases_dir = os.path.join(mgr.golden_repos_dir, "aliases")
        os.makedirs(aliases_dir, exist_ok=True)
        am = AliasManager(aliases_dir)
        am.create_alias(f"{alias}-global", old_target, repo_name=alias)
        mgr._cb_swap_alias(alias, new_target)

    def test_cow_legacy_snapshot_scheduled(self, tmp_path, stub_cleanup_manager):
        sm = _make_cow_snapshot_manager("/mnt/cow-storage")
        mgr = _make_manager(tmp_path, snapshot_manager=sm)
        old_target = "/mnt/cow-storage/myrepo/v_1699999999"
        new_target = "/mnt/cow-storage/.versioned/myrepo/v_1700000000"

        self._swap(mgr, "myrepo", old_target, new_target)

        stub_cleanup_manager.schedule_cleanup.assert_called_once_with(old_target)

    def test_master_path_preserved(self, tmp_path, stub_cleanup_manager):
        sm = _make_cow_snapshot_manager("/mnt/cow-storage")
        mgr = _make_manager(tmp_path, snapshot_manager=sm)
        # First change-branch: current_target IS the master base clone.
        master = str(tmp_path / "golden-repos" / "myrepo")
        new_target = "/mnt/cow-storage/.versioned/myrepo/v_1700000000"

        self._swap(mgr, "myrepo", master, new_target)

        stub_cleanup_manager.schedule_cleanup.assert_not_called()

    def test_change_branch_invokes_retention(
        self, tmp_path, stub_cleanup_manager, monkeypatch
    ):
        """Bug #1084 Phase A6: _cb_swap_alias runs keep-last-N retention post-swap."""
        from code_indexer.server import app as app_module

        sm = _make_cow_snapshot_manager("/mnt/cow-storage")
        mgr = _make_manager(tmp_path, snapshot_manager=sm)

        # Spy on the scheduler's _enforce_retention via the app.state lifecycle.
        lifecycle = app_module.app.state.global_lifecycle_manager
        new_target = "/mnt/cow-storage/.versioned/myrepo/v_1700000000"
        old_target = "/mnt/cow-storage/myrepo/v_1699999999"

        self._swap(mgr, "myrepo", old_target, new_target)

        lifecycle.refresh_scheduler._enforce_retention.assert_called_once_with(
            "myrepo-global", new_target
        )
