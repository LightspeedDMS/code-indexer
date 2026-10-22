"""A snapshot blocked by a live reader lease escalates once its deferral
budget is spent, instead of being re-checked silently every loop tick.

Real CleanupManager, real VersionedSnapshotManager over a LocalCloneBackend,
real snapshot directory and a real live SnapshotReaderLease. No mocks.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.snapshot_reader_lease import (
    SnapshotReaderLease,
    ensure_primary_lease_directory,
)
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

_LIVE_LEASE_TTL_SECONDS = 3600.0


def _manager(mount: Path, lease_root: Path) -> CleanupManager:
    manager = CleanupManager(
        query_tracker=QueryTracker(), min_retention_age_seconds=0.0
    )
    manager.set_snapshot_manager(
        VersionedSnapshotManager(
            versioned_base=str(mount),
            clone_backend=LocalCloneBackend(versioned_base=str(mount)),
        )
    )
    manager.set_lease_root(lease_root)
    return manager


class TestReaderLeaseDeferralBudget:
    def test_exhausted_budget_escalates_to_the_failure_path_at_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        mount = tmp_path / "cow-storage"
        snapshot = mount / ".versioned" / "example-repo" / "v_1"
        snapshot.mkdir(parents=True)
        lease_root = mount / "cidx-meta"
        lease_root.mkdir()
        ensure_primary_lease_directory(lease_root)
        lease = SnapshotReaderLease(
            str(snapshot), _LIVE_LEASE_TTL_SECONDS, lease_root=lease_root
        )
        lease.acquire()
        try:
            manager = _manager(mount, lease_root)
            manager.schedule_cleanup(str(snapshot))
            with caplog.at_level(logging.WARNING):
                for _ in range(CleanupManager.MAX_IN_USE_DEFERRALS + 1):
                    manager._next_retry_times.pop(str(snapshot), None)
                    manager._process_cleanup_queue()

            assert snapshot.is_dir()
            assert manager._get_failure_count(str(snapshot)) == 1
            assert any(
                record.levelno >= logging.WARNING
                and str(snapshot) in record.getMessage()
                for record in caplog.records
            )
        finally:
            lease.release()
