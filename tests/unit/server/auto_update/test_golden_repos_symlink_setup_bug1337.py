"""Tests for DeploymentExecutor._ensure_golden_repos_symlink_for_cow_daemon().

Bug #1337: Per-user activation on CoW-daemon clusters fails because
~/.cidx-server/data/golden-repos is provisioned as a PLAIN directory (or a
direct bind/NFS mount at that exact path), never a symlink into the CoW
mount/daemon storage tree. CowDaemonBackend._translate_to_daemon_path cannot
translate a plain directory to a daemon-local path, so
CowDaemonBackend.create_clone_at_path raises during activation.

This mirrors the Bug #1052 activated-repos symlink fix (same idempotent
check-then-apply pattern), with one difference specific to golden-repos: an
EMPTY real directory (no prior golden-repo data) is safe to convert to a
symlink automatically; a NON-EMPTY real directory (live golden-repo data) is
migrated to a `.legacy.bug1337` backup (Bug #1463) rather than touched
directly.

Bug #1464: target resolution used to special-case the co-located CoW-daemon
HOST (detected via COW_DAEMON_HOST_CONFIG_PATH) to use
{daemon_storage_path}/golden-repos instead of {mount_point}/golden-repos, on
the theory that no bind-mount indirection was needed there. That assumed the
code-indexer service account could locally traverse the daemon operator's
storage path -- false on a real staging cluster node (0700 home dir owned by
a different user), which broke golden-repo query serving on that node. The
special case is REMOVED: the target is now always {mount_point}/golden-repos,
matching the proven-correct activated-repos twin. Additionally, an existing
symlink whose target no longer matches the freshly-resolved target is now
SELF-HEALED (atomically re-pointed) rather than only warned about forever,
so any node still holding the old daemon_storage_path-form symlink converges
automatically on its next deploy cycle.

AC1: cow-daemon + link missing -> symlink created (mount_point form)
AC2: already correct symlink -> no-op (idempotent, inode unchanged)
AC3: real EMPTY directory -> auto-converted to symlink (no data at risk)
AC4: real NON-EMPTY directory -> migrated to a .legacy.bug1337 backup, symlink created
AC5: clone_backend=local -> no-op, no symlink created
AC6: cow-daemon but cow_daemon config missing/mount_point empty -> no-op + WARNING
AC7 (Bug #1464): co-located daemon host (COW_DAEMON_HOST_CONFIG_PATH present)
     -> target is STILL {mount_point}/golden-repos, never daemon_storage_path
AC8 (Bug #1464): symlink pointing to an unexpected/stale target -> atomically
     repaired to the correct target (self-heal, not warn-forever)

Real filesystem (tmp_path) used — no mocking of os.symlink or os.path.islink
(Anti-Mock rule). Only external dependencies mocked: ServerConfigManager
(config source) and _cidx_data_dir / COW_DAEMON_HOST_CONFIG_PATH (paths).
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
    daemon_storage_path: Optional[str] = None,
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
        config.cow_daemon.daemon_storage_path = daemon_storage_path
    return config


def _run_step(
    executor: DeploymentExecutor,
    data_dir: Path,
    config: MagicMock,
    daemon_host_config_path: Optional[Path] = None,
) -> bool:
    """Run _ensure_golden_repos_symlink_for_cow_daemon with patched config,
    data dir, and (optionally) the co-located daemon config path."""
    with patch(
        "code_indexer.server.utils.config_manager.ServerConfigManager"
    ) as MockCM:
        MockCM.return_value.load_config.return_value = config
        with patch(
            "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
            data_dir,
        ):
            if daemon_host_config_path is not None:
                with patch(
                    "code_indexer.server.auto_update.deployment_executor.COW_DAEMON_HOST_CONFIG_PATH",
                    daemon_host_config_path,
                ):
                    return bool(executor._ensure_golden_repos_symlink_for_cow_daemon())
            return bool(executor._ensure_golden_repos_symlink_for_cow_daemon())


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
        """clone_backend=cow-daemon + golden-repos missing -> symlink created.

        On a non-daemon-host (NFS-client) node, target is
        {mount_point}/golden-repos.
        """
        mount_point = tmp_path / "cow-storage"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        config = _make_cow_config(mount_point=str(mount_point))

        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"
        result = _run_step(
            executor, data_dir, config, daemon_host_config_path=nonexistent_daemon_cfg
        )

        link_path = data_dir_data / "golden-repos"
        assert result is True
        assert link_path.is_symlink(), "golden-repos must be a symlink"
        assert os.readlink(str(link_path)) == str(mount_point / "golden-repos"), (
            "symlink must point to {mount_point}/golden-repos on an NFS-client node"
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
        target = mount_point / "golden-repos"
        target.mkdir(parents=True)

        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        link_path = data_dir_data / "golden-repos"
        os.symlink(str(target), str(link_path))

        stat_before = os.lstat(str(link_path))

        config = _make_cow_config(mount_point=str(mount_point))
        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"
        result = _run_step(
            executor, data_dir, config, daemon_host_config_path=nonexistent_daemon_cfg
        )

        stat_after = os.lstat(str(link_path))

        assert result is True
        assert link_path.is_symlink(), "must still be a symlink"
        assert os.readlink(str(link_path)) == str(target), "target must be unchanged"
        assert stat_before.st_ino == stat_after.st_ino, (
            "inode must be unchanged (true no-op)"
        )


# ---------------------------------------------------------------------------
# AC3: real EMPTY directory -> auto-converted to symlink
# ---------------------------------------------------------------------------


class TestAutoConvertsEmptyRealDirectory:
    def test_empty_real_directory_converted_to_symlink(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """An EMPTY real directory (no prior golden-repo data) is safe to
        convert to a symlink automatically -- nothing is lost."""
        mount_point = tmp_path / "cow-storage"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"

        golden_repos_dir = data_dir_data / "golden-repos"
        golden_repos_dir.mkdir(parents=True)  # empty

        config = _make_cow_config(mount_point=str(mount_point))
        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"
        result = _run_step(
            executor, data_dir, config, daemon_host_config_path=nonexistent_daemon_cfg
        )

        assert result is True
        assert golden_repos_dir.is_symlink(), (
            "empty real directory must be converted to a symlink"
        )
        assert os.readlink(str(golden_repos_dir)) == str(mount_point / "golden-repos")


# ---------------------------------------------------------------------------
# AC4 (Bug #1463): real NON-EMPTY directory -> safely migrated to a
# `.legacy.bug1337` backup, then converted to a symlink into the CoW mount.
#
# Prior to Bug #1463, this branch only logged a WARNING and left the
# directory untouched forever -- which is exactly why the staging cluster's
# already-deployed nodes (each holding real golden-repo clone data) never
# self-healed across any number of auto-update/deploy cycles: every single
# run re-detected the same non-empty directory and re-emitted the same
# WARNING, with no forward progress. Bug #1463 requires the self-heal to
# actually perform the SAME safe migration the WARNING's own manual
# remediation text describes (mv to a `.legacy.bug1337` backup, never
# deleting data, then symlink into the CoW mount) so the fleet converges.
# ---------------------------------------------------------------------------


class TestMigratesRealDirectoryWithContentToSymlink:
    def test_real_directory_with_content_is_migrated_to_symlink(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Bug #1463: pre-existing real directory with content -> renamed to
        a `.legacy.bug1337` backup (data fully preserved, never deleted or
        modified) and golden-repos becomes a symlink into the CoW mount."""
        mount_point = tmp_path / "cow-storage"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"

        golden_repos_dir = data_dir_data / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        sentinel_file = golden_repos_dir / "metadata.json"
        sentinel_file.write_text('{"repos": []}')

        config = _make_cow_config(mount_point=str(mount_point))
        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"

        with caplog.at_level(logging.INFO):
            result = _run_step(
                executor,
                data_dir,
                config,
                daemon_host_config_path=nonexistent_daemon_cfg,
            )

        assert result is True, "step must return True (migration succeeded)"
        assert golden_repos_dir.is_symlink(), (
            "golden-repos must now be a symlink into the CoW mount"
        )
        assert os.readlink(str(golden_repos_dir)) == str(
            mount_point / "golden-repos"
        ), "symlink must point at {mount_point}/golden-repos"

        legacy_path = data_dir_data / "golden-repos.legacy.bug1337"
        legacy_sentinel = legacy_path / "metadata.json"
        assert legacy_path.exists() and legacy_path.is_dir(), (
            "the real directory's content must be preserved at a "
            ".legacy.bug1337 backup path, never deleted"
        )
        assert legacy_sentinel.exists(), "user data file must survive the migration"
        assert legacy_sentinel.read_text() == '{"repos": []}', (
            "user data must not be modified by the migration"
        )
        assert "Bug #1337" in caplog.text or "golden-repos" in caplog.text, (
            "the migration must be logged, mentioning Bug #1337 or golden-repos"
        )


# ---------------------------------------------------------------------------
# Bug #1463: a pre-existing `.legacy.bug1337` backup (e.g. from a prior
# partial/interrupted run) must NEVER be silently overwritten -- refuse
# LOUDLY (ERROR-level, per this project's Anti-Fallback principle: succeed
# cleanly or fail loudly, never a silent half-migrated/ambiguous state) and
# leave every path involved completely untouched, including never creating
# the target directory or any symlink.
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

        golden_repos_dir = data_dir_data / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        current_sentinel = golden_repos_dir / "metadata.json"
        current_sentinel.write_text('{"repos": ["current"]}')

        legacy_path = data_dir_data / "golden-repos.legacy.bug1337"
        legacy_path.mkdir(parents=True)
        legacy_sentinel = legacy_path / "old-metadata.json"
        legacy_sentinel.write_text('{"repos": ["stale-from-prior-run"]}')

        config = _make_cow_config(mount_point=str(mount_point))
        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"

        with caplog.at_level(logging.WARNING):
            result = _run_step(
                executor,
                data_dir,
                config,
                daemon_host_config_path=nonexistent_daemon_cfg,
            )

        assert result is True, "step must return True (non-fatal, handled)"

        # Nothing touched: the current directory is exactly as it was.
        assert golden_repos_dir.exists() and not golden_repos_dir.is_symlink(), (
            "current directory must NOT be touched when a backup collision is detected"
        )
        assert current_sentinel.read_text() == '{"repos": ["current"]}', (
            "current data must be untouched"
        )
        # No partial state: no symlink anywhere, no target created.
        assert not (data_dir_data / "golden-repos").is_symlink()
        assert not (mount_point / "golden-repos").exists(), (
            "the CoW-mount target must NEVER be created when the migration "
            "is refused -- no partial state left behind"
        )
        # The pre-existing backup is untouched (not overwritten/merged).
        assert legacy_sentinel.read_text() == '{"repos": ["stale-from-prior-run"]}', (
            "pre-existing backup must NOT be overwritten or merged"
        )
        assert not (legacy_path / "metadata.json").exists(), (
            "current directory's content must NOT have been merged into "
            "the pre-existing backup"
        )
        # Fails LOUDLY: an ERROR-level record, not just a WARNING.
        assert any(r.levelno == logging.ERROR for r in caplog.records), (
            "a backup collision must be refused with an ERROR-level log "
            "(Anti-Fallback: fail loudly, never silently), not merely a WARNING"
        )


# ---------------------------------------------------------------------------
# Bug #1463: the CoW-mount target may already hold content (e.g. another
# cluster node already migrated its own local copy into the SAME shared
# NFS/CoW-mount target). Migration must never write into / overwrite target
# content -- it only backs up the LOCAL directory and symlinks to whatever
# is already at target. This mirrors this codebase's established
# collision-handling convention (_ensure_single_nfs_symlink: `if not
# dest.exists(): shutil.move(...)` -- pre-existing target content always
# wins, never overwritten).
# ---------------------------------------------------------------------------


class TestMigrationIsSafeWhenTargetAlreadyHasContent:
    def test_target_preexisting_content_is_never_touched(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
    ) -> None:
        mount_point = tmp_path / "cow-storage"
        target = mount_point / "golden-repos"
        target.mkdir(parents=True)
        target_sentinel = target / "shared-metadata.json"
        target_sentinel.write_text('{"repos": ["from-another-node"]}')

        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"

        golden_repos_dir = data_dir_data / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        local_sentinel = golden_repos_dir / "metadata.json"
        local_sentinel.write_text('{"repos": ["local-stale-copy"]}')

        config = _make_cow_config(mount_point=str(mount_point))
        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"

        result = _run_step(
            executor, data_dir, config, daemon_host_config_path=nonexistent_daemon_cfg
        )

        assert result is True
        assert golden_repos_dir.is_symlink(), (
            "golden-repos must become a symlink pointing at the shared target"
        )
        assert os.readlink(str(golden_repos_dir)) == str(target)
        assert target_sentinel.read_text() == '{"repos": ["from-another-node"]}', (
            "pre-existing shared target content must be completely untouched"
        )
        assert not (target / "metadata.json").exists(), (
            "local content must NOT have been merged/copied into target"
        )

        legacy_path = data_dir_data / "golden-repos.legacy.bug1337"
        legacy_local_sentinel = legacy_path / "metadata.json"
        assert legacy_local_sentinel.exists(), (
            "the local node's own data must still be preserved as a backup"
        )
        assert legacy_local_sentinel.read_text() == '{"repos": ["local-stale-copy"]}'


# ---------------------------------------------------------------------------
# AC5: clone_backend != cow-daemon -> no-op
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

        link_path = data_dir_data / "golden-repos"
        assert result is True
        assert not link_path.exists(), "no symlink must be created for local backend"


# ---------------------------------------------------------------------------
# AC6: cow-daemon backend but cow_daemon config missing/invalid -> no-op + warning
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

        link_path = data_dir_data / "golden-repos"
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
        config.cow_daemon = MagicMock()
        config.cow_daemon.mount_point = ""
        config.cow_daemon.daemon_storage_path = None

        with caplog.at_level(logging.WARNING):
            result = _run_step(executor, data_dir, config)

        link_path = data_dir_data / "golden-repos"
        assert result is True, "must return True (non-fatal)"
        assert not link_path.exists(), (
            "no symlink must be created when mount_point empty"
        )
        assert any(r.levelno >= logging.WARNING for r in caplog.records), (
            "a WARNING must be logged when mount_point is empty"
        )


# ---------------------------------------------------------------------------
# AC7 (Bug #1464): the daemon-host special case is REMOVED -- target is
# ALWAYS the mount_point form, even on the co-located CoW-daemon host. The
# prior daemon_storage_path form assumed the code-indexer service account
# could locally traverse the daemon operator's storage path; on a real
# staging cluster node this was a 0700 directory owned by a different user,
# breaking golden-repo query serving. The activated-repos twin
# (_ensure_activated_repos_symlink_for_cow_daemon) never had this special
# case and is proven correct on the daemon host too -- this fix aligns
# golden-repos with that proven-correct unconditional behavior.
# ---------------------------------------------------------------------------


class TestResolveTargetAlwaysUsesMountPointBug1464:
    def test_resolve_target_ignores_daemon_host_config_and_daemon_storage_path(
        self, tmp_path: Path
    ) -> None:
        """Direct unit test of the static resolver: even when
        COW_DAEMON_HOST_CONFIG_PATH exists (co-located daemon host) AND
        cow_cfg.daemon_storage_path is set, the resolved target must be
        {mount_point}/golden-repos -- never the daemon_storage_path form."""
        mount_point = tmp_path / "mnt-cow-storage"
        daemon_storage_path = tmp_path / "srv-cow-xfs"

        daemon_host_cfg = tmp_path / "cow-storage-daemon-config.json"
        daemon_host_cfg.write_text('{"base_path": "%s"}' % daemon_storage_path)

        cow_cfg = MagicMock()
        cow_cfg.mount_point = str(mount_point)
        cow_cfg.daemon_storage_path = str(daemon_storage_path)

        with patch(
            "code_indexer.server.auto_update.deployment_executor.COW_DAEMON_HOST_CONFIG_PATH",
            daemon_host_cfg,
        ):
            target = DeploymentExecutor._resolve_golden_repos_symlink_target(cow_cfg)

        assert target == mount_point / "golden-repos", (
            "target must always be {mount_point}/golden-repos, even on the "
            "co-located daemon host with a resolved daemon_storage_path"
        )


class TestNodeAwareTargetOnDaemonHost:
    def test_daemon_host_uses_daemon_storage_path_form(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """Bug #1464: on the co-located CoW-daemon host (its own config file
        present), the symlink target must still be
        {mount_point}/golden-repos -- the prior daemon_storage_path-form
        special case is removed because it assumed local traversal
        permission that does not hold on every real deployment (0700 home
        dir owned by a different user on a real staging node)."""
        mount_point = tmp_path / "mnt-cow-storage"
        daemon_storage_path = tmp_path / "srv-cow-xfs"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        daemon_host_cfg = tmp_path / "cow-storage-daemon-config.json"
        daemon_host_cfg.write_text('{"base_path": "%s"}' % daemon_storage_path)

        config = _make_cow_config(
            mount_point=str(mount_point),
            daemon_storage_path=str(daemon_storage_path),
        )

        result = _run_step(
            executor, data_dir, config, daemon_host_config_path=daemon_host_cfg
        )

        link_path = data_dir_data / "golden-repos"
        assert result is True
        assert link_path.is_symlink()
        assert os.readlink(str(link_path)) == str(mount_point / "golden-repos"), (
            "co-located daemon host must target the mount_point form, not "
            "daemon_storage_path (Bug #1464)"
        )

    def test_daemon_host_config_present_but_daemon_storage_path_unset_falls_back_to_mount_point(
        self, executor: DeploymentExecutor, tmp_path: Path
    ) -> None:
        """Co-located daemon-config file present but
        cow_daemon.daemon_storage_path not (yet) resolved in config.json ->
        still resolves to the mount_point form."""
        mount_point = tmp_path / "mnt-cow-storage"
        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        daemon_host_cfg = tmp_path / "cow-storage-daemon-config.json"
        daemon_host_cfg.write_text('{"base_path": "/srv/cow-xfs"}')

        config = _make_cow_config(
            mount_point=str(mount_point), daemon_storage_path=None
        )

        result = _run_step(
            executor, data_dir, config, daemon_host_config_path=daemon_host_cfg
        )

        link_path = data_dir_data / "golden-repos"
        assert result is True
        assert link_path.is_symlink()
        assert os.readlink(str(link_path)) == str(mount_point / "golden-repos")


# ---------------------------------------------------------------------------
# AC8 (Bug #1464): a symlink pointing at a stale/mismatched target must be
# SELF-HEALED (atomically re-pointed to the freshly-resolved target), not
# left broken with a WARNING forever. Bug #1464's Part 1 fix removed the
# only legitimate source of a target mismatch (the daemon-host special
# case), so any remaining mismatch (e.g. a node whose symlink still points
# at the old daemon_storage_path form) must self-heal on the next deploy
# cycle. The repair only ever re-points the symlink -- it must NEVER touch,
# move, or delete real directory data on either the old or new target side.
# ---------------------------------------------------------------------------


class TestReconcileSelfHealsMismatchedSymlinkBug1464:
    def test_reconcile_repairs_mismatched_symlink_to_new_target(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Direct unit test of the static reconcile method: an existing
        symlink pointing at a stale target is atomically re-pointed to the
        new target. Real data at both the old and new target directories
        must remain completely untouched -- only the symlink itself moves."""
        old_target = tmp_path / "old-daemon-local" / "golden-repos"
        old_target.mkdir(parents=True)
        old_sentinel = old_target / "some-repo-data.txt"
        old_sentinel.write_text("real data at the old (stale) target")

        new_target = tmp_path / "mnt-cow-storage" / "golden-repos"
        new_target.mkdir(parents=True)
        new_sentinel = new_target / "some-repo-data.txt"
        new_sentinel.write_text("real data at the new (correct) target")

        link_path = tmp_path / "golden-repos"
        os.symlink(str(old_target), str(link_path))

        with caplog.at_level(logging.INFO):
            result = DeploymentExecutor._reconcile_existing_golden_repos_symlink(
                link_path, new_target
            )

        assert result is True
        assert os.readlink(str(link_path)) == str(new_target), (
            "symlink must be atomically re-pointed to the new target"
        )
        assert old_sentinel.exists() and old_sentinel.read_text() == (
            "real data at the old (stale) target"
        ), "the old target's real data must never be touched, moved, or deleted"
        assert new_sentinel.exists() and new_sentinel.read_text() == (
            "real data at the new (correct) target"
        ), "the new target's real data must never be touched, moved, or deleted"

    def test_reconcile_still_noops_when_already_correct(self, tmp_path: Path) -> None:
        """Preserve the existing already-correct no-op branch unchanged."""
        target = tmp_path / "mnt-cow-storage" / "golden-repos"
        target.mkdir(parents=True)
        link_path = tmp_path / "golden-repos"
        os.symlink(str(target), str(link_path))
        stat_before = os.lstat(str(link_path))

        result = DeploymentExecutor._reconcile_existing_golden_repos_symlink(
            link_path, target
        )

        stat_after = os.lstat(str(link_path))
        assert result is True
        assert stat_before.st_ino == stat_after.st_ino, (
            "an already-correct symlink must remain a true no-op"
        )


class TestSelfHealsWhenSymlinkPointsElsewhere:
    def test_symlink_to_unexpected_target_is_repaired(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """End-to-end through the full symlink-setup step: a symlink
        pointing at a stale/incorrect target is repaired to the correct
        mount_point target, rather than warned about forever (Bug #1464)."""
        mount_point = tmp_path / "cow-storage"
        wrong_target = tmp_path / "somewhere-else"
        wrong_target.mkdir(parents=True)

        data_dir = tmp_path / ".cidx-server"
        data_dir_data = data_dir / "data"
        data_dir_data.mkdir(parents=True)

        link_path = data_dir_data / "golden-repos"
        os.symlink(str(wrong_target), str(link_path))

        config = _make_cow_config(mount_point=str(mount_point))
        nonexistent_daemon_cfg = tmp_path / "does-not-exist" / "config.json"

        with caplog.at_level(logging.INFO):
            result = _run_step(
                executor,
                data_dir,
                config,
                daemon_host_config_path=nonexistent_daemon_cfg,
            )

        assert result is True
        assert os.readlink(str(link_path)) == str(mount_point / "golden-repos"), (
            "a mismatched symlink must be repaired to the correct "
            "mount_point target, not left pointing at the stale target"
        )
