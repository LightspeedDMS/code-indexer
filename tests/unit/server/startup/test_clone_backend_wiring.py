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


# ---------------------------------------------------------------------------
# _check_daemon_health version enforcement (Commit 2)
# ---------------------------------------------------------------------------


class TestCheckDaemonHealthVersionCheck:
    """_check_daemon_health rejects daemons older than v0.2.0."""

    def _mock_health_response(self, data: dict):
        """Build a mock requests module returning given data from GET /api/v1/health."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = data
        mock_resp.raise_for_status = MagicMock()

        mock_requests = MagicMock()
        mock_requests.get.return_value = mock_resp
        return mock_requests

    def test_passes_when_version_is_0_2_0(self):
        """_check_daemon_health does not raise when daemon reports version 0.2.0."""
        import sys
        from code_indexer.server.startup.clone_backend_wiring import (
            _check_daemon_health,
        )

        mock_requests = self._mock_health_response(
            {"status": "healthy", "version": "0.2.0"}
        )
        with patch.dict(sys.modules, {"requests": mock_requests}):
            # Must not raise
            _check_daemon_health("http://daemon:8081")

    def test_raises_when_version_is_0_1_0(self):
        """_check_daemon_health raises RuntimeError when daemon reports version 0.1.0."""
        import sys
        from code_indexer.server.startup.clone_backend_wiring import (
            _check_daemon_health,
        )

        mock_requests = self._mock_health_response(
            {"status": "healthy", "version": "0.1.0"}
        )
        with patch.dict(sys.modules, {"requests": mock_requests}):
            with pytest.raises(RuntimeError, match="CIDX requires 0.2.0\\+"):
                _check_daemon_health("http://daemon:8081")

    def test_raises_when_version_field_missing(self):
        """_check_daemon_health raises RuntimeError when version field absent from response."""
        import sys
        from code_indexer.server.startup.clone_backend_wiring import (
            _check_daemon_health,
        )

        mock_requests = self._mock_health_response({"status": "healthy"})
        with patch.dict(sys.modules, {"requests": mock_requests}):
            with pytest.raises(RuntimeError, match="CIDX requires 0.2.0\\+"):
                _check_daemon_health("http://daemon:8081")

    def test_passes_when_version_is_0_2_1(self):
        """_check_daemon_health does not raise for version 0.2.1 (forward compat)."""
        import sys
        from code_indexer.server.startup.clone_backend_wiring import (
            _check_daemon_health,
        )

        mock_requests = self._mock_health_response(
            {"status": "healthy", "version": "0.2.1"}
        )
        with patch.dict(sys.modules, {"requests": mock_requests}):
            # Must not raise
            _check_daemon_health("http://daemon:8081")


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
# Bug #1337: golden-repos symlink placement validation
# ---------------------------------------------------------------------------


class TestGoldenReposSymlinkPlacementCheck:
    """Bug #1337: golden_repos_dir must be a symlink resolving under
    cow_daemon.mount_point or cow_daemon.daemon_storage_path so
    CowDaemonBackend can translate it to a daemon-local path. A plain
    directory (never a symlink) must FAIL LOUD; a dangling symlink (mount
    transiently unavailable) must degrade to a WARNING, never crash.
    """

    def test_passes_when_symlink_resolves_under_mount_point(self, tmp_path, caplog):
        from code_indexer.server.startup.clone_backend_wiring import (
            _check_golden_repos_symlink_placement,
        )

        mount_point = tmp_path / "mnt-cow-storage"
        (mount_point / "golden-repos").mkdir(parents=True)

        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.parent.mkdir(parents=True)
        golden_repos_dir.symlink_to(mount_point / "golden-repos")

        cow_cfg = MagicMock()
        cow_cfg.mount_point = str(mount_point)
        cow_cfg.daemon_storage_path = None

        # Must not raise.
        _check_golden_repos_symlink_placement(str(golden_repos_dir), cow_cfg)

    def test_passes_when_symlink_resolves_under_daemon_storage_path(
        self, tmp_path, caplog
    ):
        from code_indexer.server.startup.clone_backend_wiring import (
            _check_golden_repos_symlink_placement,
        )

        daemon_storage_path = tmp_path / "srv-cow-xfs"
        (daemon_storage_path / "golden-repos").mkdir(parents=True)

        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.parent.mkdir(parents=True)
        golden_repos_dir.symlink_to(daemon_storage_path / "golden-repos")

        cow_cfg = MagicMock()
        cow_cfg.mount_point = str(tmp_path / "mnt-cow-storage")
        cow_cfg.daemon_storage_path = str(daemon_storage_path)

        # Must not raise.
        _check_golden_repos_symlink_placement(str(golden_repos_dir), cow_cfg)

    def test_warns_when_golden_repos_dir_is_plain_directory(self, tmp_path, caplog):
        """Plain directory (never a symlink) whose realpath is not under
        mount_point or daemon_storage_path -- staging regression fix: this is
        now a non-fatal WARNING (not a raise), so snapshot_manager stays
        functional. Per-user activation will still fail at translate time
        until the golden-repos symlink migration is done -- the warning
        carries the actionable mv + ln -s remediation text."""
        import logging

        from code_indexer.server.startup.clone_backend_wiring import (
            _check_golden_repos_symlink_placement,
        )

        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        cow_cfg = MagicMock()
        cow_cfg.mount_point = str(tmp_path / "mnt-cow-storage")
        cow_cfg.daemon_storage_path = str(tmp_path / "srv-cow-xfs")

        with caplog.at_level(logging.WARNING):
            # Must NOT raise.
            _check_golden_repos_symlink_placement(str(golden_repos_dir), cow_cfg)

        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, "Expected a WARNING to be logged"
        combined = " ".join(r.getMessage() for r in warning_records)
        assert "Bug #1337" in combined
        assert "mv " in combined and "ln -s" in combined

    def test_dangling_symlink_logs_warning_and_does_not_raise(self, tmp_path, caplog):
        """Symlink present but target unresolvable (mount/CoW host down) ->
        degraded WARNING, never a hard crash (project_nfs_host_down_hangs_systemd)."""
        import logging

        from code_indexer.server.startup.clone_backend_wiring import (
            _check_golden_repos_symlink_placement,
        )

        mount_point = tmp_path / "mnt-cow-storage"  # never created -> dangling target
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.parent.mkdir(parents=True)
        golden_repos_dir.symlink_to(mount_point / "golden-repos")

        cow_cfg = MagicMock()
        cow_cfg.mount_point = str(mount_point)
        cow_cfg.daemon_storage_path = None

        with caplog.at_level(logging.WARNING):
            # Must NOT raise.
            _check_golden_repos_symlink_placement(str(golden_repos_dir), cow_cfg)

        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_build_snapshot_manager_does_not_raise_for_plain_golden_repos_dir(
        self, tmp_path, caplog
    ):
        """Integration (staging regression fix): build_snapshot_manager does
        NOT raise for a plain (never-symlinked) golden-repos dir under
        cow-daemon -- it logs a WARNING and still returns a working
        VersionedSnapshotManager with a constructed CloneBackend. Per-user
        activation will fail later at translate time (the pre-existing
        #1337 symptom, surfaced by the warning), but nothing NEW breaks."""
        import logging

        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.mkdir(parents=True)

        cfg = _make_cow_daemon_config(mount_point=str(tmp_path / "mnt-cow-storage"))
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

            with caplog.at_level(logging.WARNING):
                manager = build_snapshot_manager(
                    cfg, versioned_base=str(golden_repos_dir)
                )

        assert isinstance(manager, VersionedSnapshotManager)
        assert isinstance(manager._clone_backend, CowDaemonBackend)
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Bug #1337" in r.getMessage() for r in warning_records)

    def test_build_snapshot_manager_does_not_raise_for_dangling_symlink(self, tmp_path):
        """Integration: a dangling golden-repos symlink degrades to a WARNING;
        build_snapshot_manager still returns a working manager."""
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        mount_point = tmp_path / "mnt-cow-storage"  # never created -> dangling
        golden_repos_dir = tmp_path / "data" / "golden-repos"
        golden_repos_dir.parent.mkdir(parents=True)
        golden_repos_dir.symlink_to(mount_point / "golden-repos")

        cfg = _make_cow_daemon_config(mount_point=str(mount_point))
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

            manager = build_snapshot_manager(cfg, versioned_base=str(golden_repos_dir))

        assert isinstance(manager, VersionedSnapshotManager)


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


# ---------------------------------------------------------------------------
# versioned_base propagation (Bug fix: Story #1034 Commit 0)
# ---------------------------------------------------------------------------


class TestVersionedBasePropagation:
    def test_local_backend_get_snapshot_path_respects_versioned_base(self, tmp_path):
        """get_snapshot_path must return a path rooted under versioned_base, not /.

        Bug: build_snapshot_manager called VersionedSnapshotManager(clone_backend=backend)
        without passing versioned_base=, so _versioned_base defaulted to "" and
        get_snapshot_path returned /.versioned/{alias}/v_{ts} (filesystem root).
        """
        from code_indexer.server.startup.clone_backend_wiring import (
            build_snapshot_manager,
        )

        versioned_base = str(tmp_path)
        cfg = _make_local_config()
        manager = build_snapshot_manager(cfg, versioned_base=versioned_base)

        snapshot_path = manager.get_snapshot_path("my-alias", "1700000000")

        # Must be rooted under versioned_base, NOT under filesystem root
        assert snapshot_path.startswith(versioned_base), (
            f"Expected path under {versioned_base!r}, got {snapshot_path!r}"
        )
        assert not snapshot_path.startswith("/.versioned/"), (
            f"Path must not be rooted at filesystem root: {snapshot_path!r}"
        )


# ---------------------------------------------------------------------------
# Commit 1: Inject snapshot_manager / clone_backend into services
# ---------------------------------------------------------------------------


class TestCommit1Injection:
    """Commit 1: Verify new optional params exist on constructors with None defaults."""

    def test_refresh_scheduler_accepts_snapshot_manager_param(self, tmp_path):
        """RefreshScheduler.__init__ must accept snapshot_manager=None without error."""
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.global_repos.cleanup_manager import CleanupManager
        from code_indexer.global_repos.global_registry import GlobalRegistry
        from code_indexer.config import ConfigManager

        qt = QueryTracker()
        cm = CleanupManager(qt)
        cfg = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        reg = GlobalRegistry(str(tmp_path))
        # Must not raise — snapshot_manager=None is the backward-compat default
        sched = RefreshScheduler(
            golden_repos_dir=str(tmp_path),
            config_source=cfg,
            query_tracker=qt,
            cleanup_manager=cm,
            registry=reg,
            snapshot_manager=None,
        )
        assert sched._snapshot_manager is None

    def test_refresh_scheduler_stores_snapshot_manager(self, tmp_path):
        """RefreshScheduler must store the injected snapshot_manager on self._snapshot_manager."""
        from unittest.mock import MagicMock
        from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
        from code_indexer.global_repos.query_tracker import QueryTracker
        from code_indexer.global_repos.cleanup_manager import CleanupManager
        from code_indexer.global_repos.global_registry import GlobalRegistry
        from code_indexer.config import ConfigManager

        qt = QueryTracker()
        cm = CleanupManager(qt)
        cfg = ConfigManager(tmp_path / ".code-indexer" / "config.json")
        reg = GlobalRegistry(str(tmp_path))
        mock_sm = MagicMock()
        sched = RefreshScheduler(
            golden_repos_dir=str(tmp_path),
            config_source=cfg,
            query_tracker=qt,
            cleanup_manager=cm,
            registry=reg,
            snapshot_manager=mock_sm,
        )
        assert sched._snapshot_manager is mock_sm

    def test_global_repos_lifecycle_manager_accepts_snapshot_manager_param(
        self, tmp_path
    ):
        """GlobalReposLifecycleManager.__init__ must accept snapshot_manager=None without error."""
        from code_indexer.server.lifecycle.global_repos_lifecycle import (
            GlobalReposLifecycleManager,
        )

        mgr = GlobalReposLifecycleManager(
            str(tmp_path),
            snapshot_manager=None,
        )
        assert mgr.refresh_scheduler._snapshot_manager is None

    def test_global_repos_lifecycle_manager_forwards_snapshot_manager(self, tmp_path):
        """GlobalReposLifecycleManager must forward snapshot_manager to RefreshScheduler."""
        from unittest.mock import MagicMock
        from code_indexer.server.lifecycle.global_repos_lifecycle import (
            GlobalReposLifecycleManager,
        )

        mock_sm = MagicMock()
        mgr = GlobalReposLifecycleManager(
            str(tmp_path),
            snapshot_manager=mock_sm,
        )
        assert mgr.refresh_scheduler._snapshot_manager is mock_sm

    def test_activated_repo_manager_accepts_clone_backend_param(self, tmp_path):
        """ActivatedRepoManager.__init__ must accept clone_backend=None without error."""
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        arm = ActivatedRepoManager(
            data_dir=str(tmp_path),
            clone_backend=None,
        )
        assert arm._clone_backend is None

    def test_activated_repo_manager_stores_clone_backend(self, tmp_path):
        """ActivatedRepoManager must store the injected clone_backend on self._clone_backend."""
        from unittest.mock import MagicMock
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        mock_backend = MagicMock()
        arm = ActivatedRepoManager(
            data_dir=str(tmp_path),
            clone_backend=mock_backend,
        )
        assert arm._clone_backend is mock_backend
