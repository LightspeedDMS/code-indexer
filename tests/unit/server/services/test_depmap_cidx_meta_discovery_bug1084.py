"""Dep-map cidx-meta read via the discovery API (Bug #1084 B3).

``DependencyMapService._get_cidx_meta_read_path`` scanned
``golden-repos/.versioned/cidx-meta/v_*`` first and, on the cow-daemon backend
(where that local dir never exists), fell back to the LIVE base clone -- which is
also the write target. Self-consistent today, but it reads a different version
than semantic search whenever cidx-meta is alias-repointed to a snapshot under
the daemon mount.

Phase B routes the versioned-candidate discovery through the discovery API
(``VersionedSnapshotManager.latest_snapshot`` reachable via the golden-repos
manager's ``_snapshot_manager``). The local filesystem fast-path is preserved
(existing tests), and the no-snapshot fallback still returns the live path so the
documented read==write self-consistency holds.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(golden_repos_dir: str, snapshot_manager=None):
    manager = Mock()
    manager.golden_repos_dir = golden_repos_dir
    # Explicitly control _snapshot_manager: a real object (with latest_snapshot)
    # or None. Without this, a bare Mock() yields a truthy child Mock.
    manager._snapshot_manager = snapshot_manager
    manager.get_actual_repo_path.return_value = golden_repos_dir + "/cidx-meta"
    return DependencyMapService(
        golden_repos_manager=manager,
        config_manager=Mock(),
        tracking_backend=Mock(),
        analyzer=Mock(),
    )


class TestCowDaemonReadPathViaDiscoveryAPI:
    def test_uses_discovery_api_snapshot_when_no_local_versioned_dir(self):
        """cow-daemon: no local .versioned dir -> read the discovery-API snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            # Snapshot lives under a daemon mount, NOT under golden-repos/.versioned.
            mount = Path(tmp) / "mnt" / "cow-storage"
            snapshot = mount / ".versioned" / "cidx-meta" / "v_1717000000"
            snapshot.mkdir(parents=True)

            snap_mgr = Mock()
            snap_mgr.latest_snapshot.return_value = str(snapshot)

            service = _make_service(
                str(Path(tmp) / "golden-repos"), snapshot_manager=snap_mgr
            )
            # golden-repos exists but has NO .versioned/cidx-meta dir.
            (Path(tmp) / "golden-repos").mkdir(parents=True, exist_ok=True)

            result = service._get_cidx_meta_read_path()

            assert result == snapshot
            snap_mgr.latest_snapshot.assert_called_once_with("cidx-meta")
            # The mutable base clone must NOT be consulted when a snapshot exists.
            service._golden_repos_manager.get_actual_repo_path.assert_not_called()

    def test_falls_back_to_live_when_discovery_api_returns_none(self):
        """cow-daemon with no snapshot yet -> live base clone (read==write)."""
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden-repos"
            golden.mkdir(parents=True)

            snap_mgr = Mock()
            snap_mgr.latest_snapshot.return_value = None

            service = _make_service(str(golden), snapshot_manager=snap_mgr)
            live = golden / "cidx-meta"
            live.mkdir()
            service._golden_repos_manager.get_actual_repo_path.return_value = str(live)

            result = service._get_cidx_meta_read_path()

            # No snapshot -> live path (self-consistent with the write path).
            assert result == live

    def test_discovery_api_failure_degrades_to_live(self):
        """A discovery-API exception must degrade to the live path, never raise."""
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden-repos"
            golden.mkdir(parents=True)

            snap_mgr = Mock()
            snap_mgr.latest_snapshot.side_effect = RuntimeError("daemon down")

            service = _make_service(str(golden), snapshot_manager=snap_mgr)

            result = service._get_cidx_meta_read_path()

            assert result == golden / "cidx-meta"


class TestLocalFastPathPreserved:
    def test_local_versioned_dir_short_circuits_before_discovery_api(self):
        """Local: existing .versioned/cidx-meta/v_* wins; discovery API not called."""
        with tempfile.TemporaryDirectory() as tmp:
            versioned_path = os.path.join(
                tmp, "golden-repos", ".versioned", "cidx-meta", "v_1700000000"
            )
            os.makedirs(versioned_path)

            snap_mgr = Mock()
            service = _make_service(
                os.path.join(tmp, "golden-repos"), snapshot_manager=snap_mgr
            )

            result = service._get_cidx_meta_read_path()

            assert result == Path(versioned_path)
            # Local fast-path: discovery API and get_actual_repo_path untouched.
            snap_mgr.latest_snapshot.assert_not_called()
            service._golden_repos_manager.get_actual_repo_path.assert_not_called()

    def test_no_snapshot_manager_attribute_still_falls_back(self):
        """When the manager exposes no real _snapshot_manager, fall back cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            golden = Path(tmp) / "golden-repos"
            golden.mkdir(parents=True)
            live = golden / "cidx-meta"
            live.mkdir()

            service = _make_service(str(golden), snapshot_manager=None)
            service._golden_repos_manager.get_actual_repo_path.return_value = str(live)

            result = service._get_cidx_meta_read_path()

            assert result == live
