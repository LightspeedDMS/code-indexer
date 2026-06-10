"""Bug #1084 review fix: Defect-E restore reads a snapshot path that EXISTS.

The review finding: ``_list_cow_daemon_snapshots`` emitted a LEGACY-shaped path
(``{mount}/{ns}/v_<ts>``) that does not exist on disk, so
``RefreshScheduler._restore_master_from_versioned`` fed a non-existent ``cp``
source and the reverse-clone failed. The fix makes discovery emit the CANONICAL
``{mount}/.versioned/{ns}/v_<ts>`` shape — the path that actually exists.

The pre-existing Defect-E test mocked BOTH ``latest_snapshot`` and
``_clone_backend``, so it never proved the discovered path exists on disk. These
tests are end-to-end against REAL components:

- A tmpdir-simulated mount holds a REAL canonical snapshot directory with a file.
- ``test_restore_uses_existing_canonical_path_real_local_backend`` uses a REAL
  ``VersionedSnapshotManager`` over a REAL ``LocalCloneBackend`` — real glob
  discovery + real ``cp`` reverse-clone — and asserts the master materializes
  with the snapshot's content (only ``cidx fix-config`` subprocess is avoided).
- ``test_cow_daemon_discovery_path_exists_on_mount`` proves the actual buggy
  backend: REAL ``CowDaemonBackend`` discovery (requests-stubbed) yields a
  canonical path that EXISTS on the same simulated mount — the precise condition
  Defect-E requires.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.storage.shared.clone_backend import (
    CowDaemonBackend,
    LocalCloneBackend,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.utils.config_manager import CowDaemonConfig


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


def _seed_canonical_snapshot(mount: Path, ns: str, ts: int, content: str) -> Path:
    """Create a REAL canonical snapshot dir on the simulated mount, return its path."""
    snap = mount / ".versioned" / ns / f"v_{ts}"
    snap.mkdir(parents=True)
    (snap / "marker.txt").write_text(content)
    return snap


class TestDefectERestoreRealBackend:
    def test_restore_uses_existing_canonical_path_real_local_backend(
        self, golden_repos_dir, tmp_path
    ):
        """End-to-end: REAL discovery -> existing canonical path -> REAL cp restore.

        With a REAL LocalCloneBackend whose discovery globs a real canonical
        snapshot, _restore_master_from_versioned must (a) discover a path that
        EXISTS and (b) successfully reverse-clone it into the master via a real
        cp, so the master ends up holding the snapshot's content.
        """
        mount = tmp_path / "mount"
        mount.mkdir()
        ns = "my-repo"
        # Two real canonical snapshots; the newest (v_1700009999) is the source.
        _seed_canonical_snapshot(mount, ns, 1700000000, "old")
        newest = _seed_canonical_snapshot(mount, ns, 1700009999, "NEWEST-CONTENT")

        backend = LocalCloneBackend(versioned_base=str(mount))
        manager = VersionedSnapshotManager(
            clone_backend=backend, versioned_base=str(mount)
        )

        # Sanity: REAL discovery returns the canonical path AND it exists on disk.
        discovered = manager.latest_snapshot("my-repo-global")
        assert discovered == str(newest)
        assert Path(discovered).exists(), "discovered snapshot path must exist on disk"

        sched = _make_scheduler(golden_repos_dir, manager)
        master_path = golden_repos_dir / "my-repo"

        # The reverse-clone `cp` MUST run for real (it proves the source exists and
        # is readable). Only the `cidx fix-config` call is short-circuited. Because
        # refresh_scheduler and clone_backend share the one `subprocess` module
        # object, we patch with a PASSTHROUGH: real cp, stubbed cidx.
        import code_indexer.global_repos.refresh_scheduler as rs

        real_run = rs.subprocess.run

        def _passthrough_run(cmd, *args, **kwargs):
            if cmd and cmd[0] == "cidx":
                return MagicMock(returncode=0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        with patch.object(rs.subprocess, "run", side_effect=_passthrough_run):
            result = sched._restore_master_from_versioned("my-repo-global", master_path)

        assert result is True
        # Master was materialized from the EXISTING canonical snapshot via real cp.
        assert (master_path / "marker.txt").read_text() == "NEWEST-CONTENT"

    def test_cow_daemon_discovery_path_exists_on_mount(self, tmp_path):
        """The actual buggy backend: REAL CowDaemonBackend discovery (requests
        stubbed) yields a CANONICAL path that EXISTS on the simulated mount.

        This is the precondition Defect-E needs and the legacy shape violated: the
        legacy ``{mount}/{ns}/v_<ts>`` path would NOT exist (the real snapshot is
        under ``.versioned``).
        """
        mount = tmp_path / "mount"
        mount.mkdir()
        ns = "my_repo"  # sanitized daemon namespace
        snap = _seed_canonical_snapshot(mount, ns, 1700009999, "x")

        backend = CowDaemonBackend(
            config=CowDaemonConfig(
                daemon_url="http://daemon:8081",
                api_key="k",
                mount_point=str(mount),
            )
        )
        manager = VersionedSnapshotManager(clone_backend=backend)

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"namespace": ns, "name": "v_1700009999"}]
        resp.raise_for_status = MagicMock()
        mock_req = MagicMock()
        mock_req.get.return_value = resp

        with patch.dict(sys.modules, {"requests": mock_req}):
            discovered = manager.latest_snapshot("my-repo-global")

        assert discovered == str(snap)
        assert Path(discovered).exists()
        # The legacy shape would have pointed here — and it does NOT exist.
        legacy = mount / ns / "v_1700009999"
        assert not legacy.exists()
