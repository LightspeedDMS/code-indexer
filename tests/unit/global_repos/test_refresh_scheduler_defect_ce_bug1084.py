"""Bug #1084 Phase A7: Defect C + Defect E use the discovery API, not a
golden_repos_dir/.versioned glob.

Defect C (`_has_local_changes`): on cow-daemon the snapshots live under the NFS
mount, NOT under golden_repos_dir/.versioned, so the old glob always missed and
returned True ("first version") every cycle -> spurious re-index + snapshot.

Defect E (`_restore_master_from_versioned`): same glob meant a lost master could
not be restored on cow-daemon even though snapshots exist at the mount.
"""

import os
import time
from unittest.mock import MagicMock

import pytest

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


class TestDefectCHasLocalChanges:
    """`_has_local_changes` uses snapshot_manager.list_snapshots, not the glob."""

    def test_cow_layout_no_changes_returns_false(self, golden_repos_dir, tmp_path):
        """Old snapshot ts >> source mtime -> no changes -> False (no spurious snapshot)."""
        # Source files with mtime in the PAST relative to the snapshot ts.
        source = tmp_path / "live"
        source.mkdir()
        f = source / "file.txt"
        f.write_text("x")
        old_mtime = time.time() - 10_000
        os.utime(f, (old_mtime, old_mtime))

        # Snapshot recorded at a MUCH later ts than the source mtime.
        future_ts = int(time.time()) + 10_000
        sm = MagicMock()
        sm.list_snapshots.return_value = [
            (f"/mnt/cow-storage/.versioned/cidx-meta/v_{future_ts}", future_ts)
        ]
        sched = _make_scheduler(golden_repos_dir, sm)

        result = sched._has_local_changes(str(source), "cidx-meta-global")

        assert result is False
        sm.list_snapshots.assert_called_once_with("cidx-meta-global")

    def test_cow_layout_with_changes_returns_true(self, golden_repos_dir, tmp_path):
        """Source mtime newer than the latest snapshot ts -> changes -> True."""
        source = tmp_path / "live"
        source.mkdir()
        f = source / "file.txt"
        f.write_text("x")
        # Fresh mtime (now).
        now = time.time()
        os.utime(f, (now, now))

        old_ts = int(now) - 10_000
        sm = MagicMock()
        sm.list_snapshots.return_value = [
            (f"/mnt/cow-storage/.versioned/cidx-meta/v_{old_ts}", old_ts)
        ]
        sched = _make_scheduler(golden_repos_dir, sm)

        result = sched._has_local_changes(str(source), "cidx-meta-global")

        assert result is True

    def test_no_snapshots_returns_true_first_version(self, golden_repos_dir, tmp_path):
        """No snapshots at all -> first version needed -> True."""
        source = tmp_path / "live"
        source.mkdir()
        (source / "f.txt").write_text("x")

        sm = MagicMock()
        sm.list_snapshots.return_value = []
        sched = _make_scheduler(golden_repos_dir, sm)

        assert sched._has_local_changes(str(source), "cidx-meta-global") is True

    def test_local_glob_fallback_when_no_snapshot_manager(
        self, golden_repos_dir, tmp_path
    ):
        """Backward compat: with NO snapshot_manager, falls back to the local
        golden_repos_dir/.versioned glob (existing behavior preserved)."""
        # Create a local .versioned snapshot with an old ts.
        ns_dir = golden_repos_dir / ".versioned" / "cidx-meta"
        ns_dir.mkdir(parents=True)
        old_ts = int(time.time()) - 10_000
        (ns_dir / f"v_{old_ts}").mkdir()

        source = tmp_path / "live"
        source.mkdir()
        f = source / "file.txt"
        f.write_text("x")
        now = time.time()
        os.utime(f, (now, now))

        sched = _make_scheduler(golden_repos_dir, snapshot_manager=None)

        # Newer source mtime than the old snapshot -> changes detected.
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
