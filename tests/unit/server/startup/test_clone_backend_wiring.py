"""
Unit tests for clone_backend_wiring.build_snapshot_manager() helper.

Story #510 AC8 — lifespan.py wiring of CloneBackend into VersionedSnapshotManager.

build_snapshot_manager() is a pure, independently-testable factory that reads
config and constructs the appropriate VersionedSnapshotManager.  It is tested
here in isolation; lifespan.py merely calls it.

Mock justification:
- HTTP daemon health check (CowDaemonBackend): external process, not available in CI.
- NfsMountValidator: requires a real mounted NFS path, not available in CI.
- OntapFlexCloneClient: requires a live ONTAP cluster, not available in CI.
All other logic (config branching, factory selection, error propagation) uses
real code with no mocking.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.storage.shared.clone_backend import (
    CowDaemonBackend,
    LocalCloneBackend,
    OntapCloneBackend,
)
from code_indexer.server.storage.shared.snapshot_manager import VersionedSnapshotManager
from code_indexer.server.utils.config_manager import CowDaemonConfig


def _make_local_config() -> MagicMock:
    """Minimal config with clone_backend='local'."""
    cfg = MagicMock()
    cfg.clone_backend = "local"
    cfg.cow_daemon = None
    cfg.ontap = None
    return cfg


def _make_cow_daemon_config(
    daemon_url: str = "http://localhost:8081",
    api_key: str = "secret",
    mount_point: str = "/mnt/nfs",
) -> MagicMock:
    """Minimal config with clone_backend='cow-daemon'."""
    cfg = MagicMock()
    cfg.clone_backend = "cow-daemon"
    cfg.cow_daemon = CowDaemonConfig(
        daemon_url=daemon_url,
        api_key=api_key,
        mount_point=mount_point,
    )
    cfg.ontap = None
    return cfg


def _make_ontap_config() -> MagicMock:
    """Minimal config with clone_backend='ontap'."""
    from code_indexer.server.utils.config_manager import OntapConfig

    cfg = MagicMock()
    cfg.clone_backend = "ontap"
    cfg.cow_daemon = None
    cfg.ontap = OntapConfig(
        endpoint="https://ontap.example.com",
        svm_name="svm1",
        parent_volume="cidx_vol",
        mount_point="/mnt/fsx",
        admin_user="fsxadmin",
        admin_password="pass",
    )
    return cfg


# ---------------------------------------------------------------------------
# local backend
# ---------------------------------------------------------------------------


class TestLocalBackend:
    def test_local_backend_returns_snapshot_manager(self, tmp_path):
        """build_snapshot_manager returns VersionedSnapshotManager for local backend."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_local_config()
        manager = build_snapshot_manager(cfg, versioned_base=str(tmp_path))

        assert isinstance(manager, VersionedSnapshotManager)

    def test_local_backend_clone_backend_is_local_clone_backend(self, tmp_path):
        """With local config, VersionedSnapshotManager._clone_backend is a LocalCloneBackend."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_local_config()
        manager = build_snapshot_manager(cfg, versioned_base=str(tmp_path))

        assert isinstance(manager._clone_backend, LocalCloneBackend)

    def test_local_backend_does_not_use_flexclone(self, tmp_path):
        """With local config, VersionedSnapshotManager._flexclone is None."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_local_config()
        manager = build_snapshot_manager(cfg, versioned_base=str(tmp_path))

        assert manager._flexclone is None


# ---------------------------------------------------------------------------
# cow-daemon backend (healthy)
# ---------------------------------------------------------------------------


class TestCowDaemonBackendHealthy:
    def test_cow_daemon_returns_snapshot_manager(self, tmp_path):
        """build_snapshot_manager returns VersionedSnapshotManager for cow-daemon backend."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_cow_daemon_config(mount_point=str(tmp_path))
        with (
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_daemon_health"
            ) as mock_health,
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_nfs_mount"
            ) as mock_nfs,
        ):
            mock_health.return_value = None
            mock_nfs.return_value = None

            manager = build_snapshot_manager(cfg, versioned_base=str(tmp_path))

        assert isinstance(manager, VersionedSnapshotManager)

    def test_cow_daemon_backend_is_cow_daemon_backend(self, tmp_path):
        """With cow-daemon config, _clone_backend is CowDaemonBackend."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_cow_daemon_config(mount_point=str(tmp_path))
        with (
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_daemon_health"
            ) as mock_health,
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_nfs_mount"
            ) as mock_nfs,
        ):
            mock_health.return_value = None
            mock_nfs.return_value = None

            manager = build_snapshot_manager(cfg, versioned_base=str(tmp_path))

        assert isinstance(manager._clone_backend, CowDaemonBackend)

    def test_cow_daemon_calls_health_check(self, tmp_path):
        """build_snapshot_manager calls _check_daemon_health for cow-daemon backend."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_cow_daemon_config(
            daemon_url="http://storage:8081",
            mount_point=str(tmp_path),
        )
        with (
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_daemon_health"
            ) as mock_health,
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_nfs_mount"
            ) as mock_nfs,
        ):
            mock_health.return_value = None
            mock_nfs.return_value = None

            build_snapshot_manager(cfg, versioned_base=str(tmp_path))

        mock_health.assert_called_once_with("http://storage:8081")

    def test_cow_daemon_calls_nfs_check(self, tmp_path):
        """build_snapshot_manager calls _check_nfs_mount for cow-daemon backend."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_cow_daemon_config(mount_point=str(tmp_path))
        with (
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_daemon_health"
            ) as mock_health,
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_nfs_mount"
            ) as mock_nfs,
        ):
            mock_health.return_value = None
            mock_nfs.return_value = None

            build_snapshot_manager(cfg, versioned_base=str(tmp_path))

        mock_nfs.assert_called_once_with(str(tmp_path))


# ---------------------------------------------------------------------------
# cow-daemon backend (unhealthy — fail-fast)
# ---------------------------------------------------------------------------


class TestCowDaemonBackendFailFast:
    def test_raises_runtime_error_when_daemon_unreachable(self, tmp_path):
        """build_snapshot_manager raises RuntimeError if daemon health check fails."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_cow_daemon_config(mount_point=str(tmp_path))
        with (
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_daemon_health",
                side_effect=RuntimeError(
                    "CoW daemon not reachable at http://localhost:8081"
                ),
            ),
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_nfs_mount"
            ) as mock_nfs,
        ):
            mock_nfs.return_value = None

            with pytest.raises(RuntimeError, match="CoW daemon"):
                build_snapshot_manager(cfg, versioned_base=str(tmp_path))

    def test_raises_runtime_error_when_nfs_not_mounted(self, tmp_path):
        """build_snapshot_manager raises RuntimeError if NFS mount is not available."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_cow_daemon_config(mount_point=str(tmp_path))
        with (
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_daemon_health"
            ) as mock_health,
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_nfs_mount",
                side_effect=RuntimeError("NFS mount is not healthy"),
            ),
        ):
            mock_health.return_value = None

            with pytest.raises(RuntimeError, match="NFS mount"):
                build_snapshot_manager(cfg, versioned_base=str(tmp_path))

    def test_no_fallback_when_daemon_fails(self, tmp_path):
        """build_snapshot_manager does NOT silently fall back — raises immediately."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_cow_daemon_config(mount_point=str(tmp_path))
        with (
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_daemon_health",
                side_effect=RuntimeError("CoW daemon not reachable"),
            ),
            patch(
                "code_indexer.server.startup.clone_backend_wiring._check_nfs_mount"
            ) as mock_nfs,
        ):
            mock_nfs.return_value = None
            raised = False
            try:
                build_snapshot_manager(cfg, versioned_base=str(tmp_path))
            except RuntimeError:
                raised = True

        assert raised, "Expected RuntimeError but no exception was raised"


# ---------------------------------------------------------------------------
# ontap backend
# ---------------------------------------------------------------------------


_ONTAP_CLIENT_PATH = (
    "code_indexer.server.storage.shared.ontap_flexclone_client.OntapFlexCloneClient"
)


class TestOntapBackend:
    def test_ontap_backend_returns_snapshot_manager(self):
        """build_snapshot_manager returns VersionedSnapshotManager for ontap backend."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_ontap_config()
        with patch(_ONTAP_CLIENT_PATH) as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            manager = build_snapshot_manager(cfg, versioned_base="/mnt/fsx/repos")

        assert isinstance(manager, VersionedSnapshotManager)

    def test_ontap_backend_creates_ontap_clone_backend(self):
        """With ontap config, _clone_backend is OntapCloneBackend and OntapFlexCloneClient was created."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_ontap_config()
        with patch(_ONTAP_CLIENT_PATH) as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value = mock_instance

            manager = build_snapshot_manager(cfg, versioned_base="/mnt/fsx/repos")

        # CloneBackendFactory creates OntapCloneBackend (uses _clone_backend, not _flexclone)
        assert isinstance(manager._clone_backend, OntapCloneBackend)
        # OntapFlexCloneClient was instantiated with the correct ONTAP config
        mock_client_cls.assert_called_once_with(
            endpoint="https://ontap.example.com",
            username="fsxadmin",
            password="pass",
            svm_name="svm1",
            parent_volume="cidx_vol",
        )

    def test_ontap_backend_flexclone_attr_is_none(self):
        """With ontap config, _flexclone is None (OntapCloneBackend used via _clone_backend)."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        cfg = _make_ontap_config()
        with patch(_ONTAP_CLIENT_PATH) as mock_client_cls:
            mock_client_cls.return_value = MagicMock()
            manager = build_snapshot_manager(cfg, versioned_base="/mnt/fsx/repos")

        assert manager._flexclone is None
