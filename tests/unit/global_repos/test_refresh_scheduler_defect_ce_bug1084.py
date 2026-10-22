"""Bug #1084 Phase A7: Defect C + Defect E use the discovery API, not a
golden_repos_dir/.versioned glob.

Defect C (`_has_local_changes`): on cow-daemon the snapshots live under the NFS
mount, NOT under golden_repos_dir/.versioned, so the old glob always missed and
returned True ("first version") every cycle -> spurious re-index + snapshot.
The baseline is now the alias's published snapshot, read where it lives.

Defect E (`_restore_master_from_versioned`): same glob meant a lost master could
not be restored on cow-daemon even though snapshots exist at the mount.
"""

import shutil
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.local_source_manifest import record_source_manifest
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


def _make_scheduler(golden_repos_dir, snapshot_manager):
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=MagicMock(),
        snapshot_manager=snapshot_manager,
    )


class _CowSnapshotPredicate:
    """Recognizes one cow-mount snapshot path; records any listing call."""

    def __init__(self, snapshot) -> None:
        self._snapshot = str(snapshot)
        self.listed = 0

    def is_versioned_snapshot(self, path: str) -> bool:
        return path == self._snapshot

    def list_snapshots(self, alias_name: str):
        self.listed += 1
        return []


class TestDefectCHasLocalChanges:
    """`_has_local_changes` reads the baseline manifest from the alias's
    published snapshot directory wherever the backend keeps it (cow-daemon:
    under the NFS mount, not golden_repos_dir/.versioned) -- no listing."""

    def _published_cow_snapshot(self, tmp_path):
        source = tmp_path / "live"
        source.mkdir()
        (source / ".code-indexer").mkdir()
        (source / "file.txt").write_text("x")
        record_source_manifest(str(source), None)
        snapshot = tmp_path / "cow-storage" / ".versioned" / "cidx-meta" / "v_5"
        shutil.copytree(source, snapshot)
        return source, snapshot, _CowSnapshotPredicate(snapshot)

    def test_cow_layout_no_changes_returns_false(self, golden_repos_dir, tmp_path):
        source, snapshot, sm = self._published_cow_snapshot(tmp_path)
        sched = _make_scheduler(golden_repos_dir, sm)
        sched.alias_manager.create_alias(
            "cidx-meta-global", str(snapshot), repo_name="cidx-meta"
        )

        assert sched._has_local_changes(str(source), "cidx-meta-global") is False
        assert sm.listed == 0

    def test_cow_layout_with_changes_returns_true(self, golden_repos_dir, tmp_path):
        source, snapshot, sm = self._published_cow_snapshot(tmp_path)
        (source / "file.txt").write_text("edited")
        sched = _make_scheduler(golden_repos_dir, sm)
        sched.alias_manager.create_alias(
            "cidx-meta-global", str(snapshot), repo_name="cidx-meta"
        )

        assert sched._has_local_changes(str(source), "cidx-meta-global") is True


class TestDefectERestoreMaster:
    """`_restore_master_from_versioned` restores from the latest cow-mount snapshot."""

    def test_restores_from_latest_cow_snapshot(self, golden_repos_dir, tmp_path):
        latest_snap = "/mnt/cow-storage/.versioned/my-repo/v_1700009999"
        sm = MagicMock()
        sm.latest_snapshot.return_value = latest_snap
        # The reverse clone goes through the backend's create_clone_at_path.
        backend = MagicMock()
        sm._clone_backend = backend

        sched = _make_scheduler(golden_repos_dir, sm)
        master_path = golden_repos_dir / "my-repo"

        # Avoid the real `cidx fix-config` subprocess.
        import code_indexer.global_repos.refresh_scheduler as rs

        orig_run = rs.subprocess.run
        rs.subprocess.run = MagicMock(return_value=MagicMock(returncode=0))
        try:
            result = sched._restore_master_from_versioned("my-repo-global", master_path)
        finally:
            rs.subprocess.run = orig_run

        assert result is True
        sm.latest_snapshot.assert_called_once_with("my-repo-global")
        backend.create_clone_at_path.assert_called_once()
        # Source of the reverse clone is the latest snapshot path.
        call_args = backend.create_clone_at_path.call_args
        assert call_args[0][0] == latest_snap
        assert call_args[0][1] == str(master_path)

    def test_returns_false_when_no_snapshot(self, golden_repos_dir):
        sm = MagicMock()
        sm.latest_snapshot.return_value = None
        sched = _make_scheduler(golden_repos_dir, sm)

        result = sched._restore_master_from_versioned(
            "my-repo-global", golden_repos_dir / "my-repo"
        )

        assert result is False
