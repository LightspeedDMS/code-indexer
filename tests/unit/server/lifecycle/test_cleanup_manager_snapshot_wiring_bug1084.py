"""Bug #1084 Phase A5 wiring: GlobalReposLifecycleManager must hand the
VersionedSnapshotManager to its CleanupManager so deletions are backend-correct.

This is the anti-orphan-code guard (Rule 12): set_snapshot_manager() exists on
CleanupManager only to be called here. If this wiring regresses, cleanup silently
reverts to rmtree-only and the cow-daemon / ONTAP leak returns.
"""

from unittest.mock import MagicMock

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)


def test_lifecycle_wires_snapshot_manager_into_cleanup_manager(tmp_path):
    sm = MagicMock(name="VersionedSnapshotManager")

    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        snapshot_manager=sm,
    )

    # CleanupManager must have received the snapshot manager for backend deletion.
    assert lifecycle.cleanup_manager._snapshot_manager is sm


def test_lifecycle_without_snapshot_manager_leaves_cleanup_unwired(tmp_path):
    """No snapshot_manager (e.g. misconfigured backend) -> cleanup stays rmtree-only."""
    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        snapshot_manager=None,
    )

    assert lifecycle.cleanup_manager._snapshot_manager is None
