"""Tests for DeploymentExecutor._ensure_activated_repos_symlink_for_cow_daemon().

Bug #1052: Auto-updater must idempotently set up
~/.cidx-server/data/activated-repos as a symlink to
{cow_daemon.mount_point}/activated-repos on CoW-daemon cluster nodes.

AC1: cow-daemon backend + path missing -> symlink created
AC2: already correct symlink -> no-op (idempotent)
AC3: real directory with content -> warning logged, data preserved, no symlink
AC4: clone_backend=local -> no-op, no symlink created
AC5: cow-daemon backend but cow_daemon config missing/invalid -> no-op with warning

Real filesystem (tmp_path) used — no mocking of os.symlink or os.path.islink
(Anti-Mock rule).

Only external dependencies mocked:
  - ServerConfigManager (config source)
  - _cidx_data_dir (data directory path)
"""

import logging
import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cow_config(
    clone_backend: str = "cow-daemon",
    mount_point: Optional[str] = None,
    cow_daemon_none: bool = False,
) -> MagicMock:
    """Return a mock server config for CoW-daemon scenarios."""
    config = MagicMock()
    config.clone_backend = clone_backend
    if cow_daemon_none or mount_point is None:
        config.cow_daemon = None
    else:
        config.cow_daemon = MagicMock()
        config.cow_daemon.mount_point = mount_point
    return config


def _run_step(
    executor: DeploymentExecutor,
    data_dir: Path,
    config: MagicMock,
) -> bool:
    """Run _ensure_activated_repos_symlink_for_cow_daemon with patched config and data dir."""
    with patch(
        "code_indexer.server.utils.config_manager.ServerConfigManager"
    ) as MockCM:
        MockCM.return_value.load_config.return_value = config
        with patch(
            "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
            data_dir,
        ):
            return bool(executor._ensure_activated_repos_symlink_for_cow_daemon())


@pytest.fixture()
def executor() -> DeploymentExecutor:
    """Minimal DeploymentExecutor for unit testing."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


# ---------------------------------------------------------------------------
# AC1: cow-daemon backend + path missing -> symlink created
# ---------------------------------------------------------------------------


class TestCreatesSymlinkWhenMissing:
    def test_creates_symlink_when_cow_daemon_and_path_missing(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """clone_backend=cow-daemon + activated-repos missing -> symlink created.

        After the step, ~/.cidx-server/data/activated-repos must be a symlink
        pointing to {mount_point}/activated-repos.
        """
        mount_point = tmp_path / "cow-storage"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        config = _make_cow_config(mount_point=str(mount_point))

        result = _run_step(executor, data_dir, config)

        link_path = data_dir_data / "activated-repos"
        assert result is True
        assert link_path.is_symlink(), "activated-repos must be a symlink"
        assert os.readlink(str(link_path)) == str(mount_point / "activated-repos"), (
            "symlink must point to {mount_point}/activated-repos"
        )


# ---------------------------------------------------------------------------
# AC2: already correct symlink -> no-op (idempotent)
# ---------------------------------------------------------------------------


class TestIdempotentWhenAlreadyCorrectSymlink:
    def test_noop_when_already_correct_symlink(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """Pre-existing correct symlink -> step is a no-op; inode unchanged."""
        mount_point = tmp_path / "cow-storage"
        target = mount_point / "activated-repos"
        target.mkdir(parents=True)

        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        link_path = data_dir_data / "activated-repos"
        os.symlink(str(target), str(link_path))

        # Capture lstat before
        stat_before = os.lstat(str(link_path))

        config = _make_cow_config(mount_point=str(mount_point))
        result = _run_step(executor, data_dir, config)

        stat_after = os.lstat(str(link_path))

        assert result is True
        assert link_path.is_symlink(), "must still be a symlink"
        assert os.readlink(str(link_path)) == str(target), "target must be unchanged"
        assert stat_before.st_ino == stat_after.st_ino, (
            "inode must be unchanged (true no-op)"
        )


# ---------------------------------------------------------------------------
# AC3 (Bug #1463): real directory with content -> safely migrated to a
# `.legacy.bug1052` backup, then converted to a symlink into the CoW mount.
#
# Prior to Bug #1463 this branch only WARNED and left the directory
# untouched forever -- the same fleet-wide non-convergence gap that Bug
# #1463 fixes for golden-repos (see test_golden_repos_symlink_setup_bug1337
# .py's equivalent test/docstring for the full staging-cluster rationale).
# ---------------------------------------------------------------------------


class TestMigratesRealDirectoryWithContentToSymlink:
    def test_real_directory_with_content_is_migrated_to_symlink(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Bug #1463: pre-existing real directory with content -> renamed to
        a `.legacy.bug1052` backup (data fully preserved) and
        activated-repos becomes a symlink into the CoW mount."""
        mount_point = tmp_path / "cow-storage"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"

        activated_dir = data_dir_data / "activated-repos"
        activated_dir.mkdir(parents=True)
        sentinel_file = activated_dir / "important-user-data.json"
        sentinel_file.write_text('{"workspace": "prod"}')

        config = _make_cow_config(mount_point=str(mount_point))

        with caplog.at_level(logging.INFO):
            result = _run_step(executor, data_dir, config)

        assert result is True, "step must return True (migration succeeded)"
        assert activated_dir.is_symlink(), (
            "activated-repos must now be a symlink into the CoW mount"
        )
        assert os.readlink(str(activated_dir)) == str(mount_point / "activated-repos")

        legacy_path = data_dir_data / "activated-repos.legacy.bug1052"
        legacy_sentinel = legacy_path / "important-user-data.json"
        assert legacy_path.exists() and legacy_path.is_dir(), (
            "the real directory's content must be preserved at a "
            ".legacy.bug1052 backup path, never deleted"
        )
        assert legacy_sentinel.exists(), "user data file must survive the migration"
        assert legacy_sentinel.read_text() == '{"workspace": "prod"}', (
            "user data must not be modified by the migration"
        )
        assert "Bug #1052" in caplog.text or "activated-repos" in caplog.text, (
            "the migration must be logged, mentioning Bug #1052 or activated-repos"
        )


# ---------------------------------------------------------------------------
# Bug #1463: a pre-existing `.legacy.bug1052` backup must never be silently
# overwritten -- refuse LOUDLY (ERROR) and leave every path untouched.
# ---------------------------------------------------------------------------


class TestRefusesWhenLegacyBackupAlreadyExists:
    def test_preexisting_legacy_backup_blocks_migration_and_is_not_overwritten(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mount_point = tmp_path / "cow-storage"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"

        activated_dir = data_dir_data / "activated-repos"
        activated_dir.mkdir(parents=True)
        current_sentinel = activated_dir / "important-user-data.json"
        current_sentinel.write_text('{"workspace": "current"}')

        legacy_path = data_dir_data / "activated-repos.legacy.bug1052"
        legacy_path.mkdir(parents=True)
        legacy_sentinel = legacy_path / "old-user-data.json"
        legacy_sentinel.write_text('{"workspace": "stale-from-prior-run"}')

        config = _make_cow_config(mount_point=str(mount_point))

        with caplog.at_level(logging.WARNING):
            result = _run_step(executor, data_dir, config)

        assert result is True, "step must return True (non-fatal, handled)"
        assert activated_dir.exists() and not activated_dir.is_symlink(), (
            "current directory must NOT be touched when a backup collision is detected"
        )
        assert current_sentinel.read_text() == '{"workspace": "current"}'
        assert not (mount_point / "activated-repos").exists(), (
            "the CoW-mount target must NEVER be created when the migration "
            "is refused -- no partial state left behind"
        )
        assert legacy_sentinel.read_text() == '{"workspace": "stale-from-prior-run"}', (
            "pre-existing backup must NOT be overwritten or merged"
        )
        assert any(r.levelno == logging.ERROR for r in caplog.records), (
            "a backup collision must be refused with an ERROR-level log "
            "(Anti-Fallback: fail loudly, never silently), not merely a WARNING"
        )


# ---------------------------------------------------------------------------
# Bug #1463: migration must be collision-safe when the CoW-mount target
# already has content (e.g. another cluster node already migrated).
# ---------------------------------------------------------------------------


class TestMigrationIsSafeWhenTargetAlreadyHasContent:
    def test_target_preexisting_content_is_never_touched(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        mount_point = tmp_path / "cow-storage"
        target = mount_point / "activated-repos"
        target.mkdir(parents=True)
        target_sentinel = target / "shared-user-data.json"
        target_sentinel.write_text('{"workspace": "from-another-node"}')

        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"

        activated_dir = data_dir_data / "activated-repos"
        activated_dir.mkdir(parents=True)
        local_sentinel = activated_dir / "important-user-data.json"
        local_sentinel.write_text('{"workspace": "local-stale-copy"}')

        config = _make_cow_config(mount_point=str(mount_point))

        result = _run_step(executor, data_dir, config)

        assert result is True
        assert activated_dir.is_symlink()
        assert os.readlink(str(activated_dir)) == str(target)
        assert target_sentinel.read_text() == '{"workspace": "from-another-node"}', (
            "pre-existing shared target content must be completely untouched"
        )
        assert not (target / "important-user-data.json").exists(), (
            "local content must NOT have been merged/copied into target"
        )

        legacy_path = data_dir_data / "activated-repos.legacy.bug1052"
        legacy_local_sentinel = legacy_path / "important-user-data.json"
        assert legacy_local_sentinel.exists(), (
            "the local node's own data must still be preserved as a backup"
        )
        assert legacy_local_sentinel.read_text() == '{"workspace": "local-stale-copy"}'


# ---------------------------------------------------------------------------
# AC4: clone_backend != cow-daemon -> no-op
# ---------------------------------------------------------------------------


class TestNoopWhenLocalBackend:
    def test_noop_when_clone_backend_is_local(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """clone_backend=local -> step is a no-op; no symlink created."""
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        config = _make_cow_config(clone_backend="local", mount_point="/some/mount")

        result = _run_step(executor, data_dir, config)

        link_path = data_dir_data / "activated-repos"
        assert result is True
        assert not link_path.exists(), "no symlink must be created for local backend"


# ---------------------------------------------------------------------------
# AC5: cow-daemon backend but cow_daemon config missing/invalid -> no-op + warning
# ---------------------------------------------------------------------------


class TestNoopWhenCowDaemonConfigMissing:
    def test_noop_when_cow_daemon_config_is_none(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """clone_backend=cow-daemon but cow_daemon=None -> no-op, WARNING logged, no crash."""
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        config = _make_cow_config(clone_backend="cow-daemon", cow_daemon_none=True)

        with caplog.at_level(logging.WARNING):
            result = _run_step(executor, data_dir, config)

        link_path = data_dir_data / "activated-repos"
        assert result is True, "must return True (non-fatal)"
        assert not link_path.exists(), "no symlink must be created when config missing"
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "a WARNING must be logged when cow_daemon config is absent"
        )

    def test_noop_when_mount_point_is_empty_string(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """clone_backend=cow-daemon, cow_daemon.mount_point='' -> no-op, WARNING logged."""
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        config = _make_cow_config(clone_backend="cow-daemon", mount_point="")
        # mount_point="" means cow_daemon IS set but mount_point is empty
        config.cow_daemon = MagicMock()
        config.cow_daemon.mount_point = ""

        with caplog.at_level(logging.WARNING):
            result = _run_step(executor, data_dir, config)

        link_path = data_dir_data / "activated-repos"
        assert result is True, "must return True (non-fatal)"
        assert not link_path.exists(), (
            "no symlink must be created when mount_point empty"
        )
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "a WARNING must be logged when mount_point is empty"
        )
