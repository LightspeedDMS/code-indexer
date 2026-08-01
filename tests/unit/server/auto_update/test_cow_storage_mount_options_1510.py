"""Tests for DeploymentExecutor._ensure_cow_storage_mount_options() and its
pure helper _upgrade_cow_storage_fstab_entry_to_nfsv3().

Issue #1510: a cluster-staging investigation found the shared cow-storage
NFS mount suffering chronic NFSv4 server-side lock-manager state loss
(dmesg "lost N locks" bursts), which directly caused a SQLite
`disk I/O error` during a golden-repo refresh. A first fix added `nolock`
to the existing NFSv4.1 mount alone and was empirically proven to have
ZERO effect: NFSv4 integrates locking into the protocol's own OPEN/LOCK
state machine, bypassing the separate NLM protocol `nolock` actually
controls (an NFSv3-only mechanism). This follow-up upgrades the whole
mount to NFSv3 with `nolock` (fstype=nfs, vers=3,nolock), matching this
project's existing golden-repos mount precedent (`vers=3,nolock,hard`)
and genuinely honoring client-side-only locking (safe here because
WriteLockManager already serializes writers at the application layer).

scripts/install-cidx-server.sh already writes fstype=nfs with
`vers=3,nolock` for FRESH installs. This module is the auto-updater's
idempotent self-heal for ALREADY-DEPLOYED nodes, per this project's
Auto-Updater Idempotent Deployment mandate.

Test strategy:
- The pure static helper is tested directly against real fstab-format
  strings -- no mocking at all (it does zero filesystem/subprocess I/O).
- The orchestration method reads a REAL temp file on disk (fstab_path
  param, mirroring install-cidx-server.sh's own testable `fstab_file`
  design) -- genuine filesystem I/O, not mocked away.
- Only the privileged write path (`sudo tee`, `sudo umount`, `sudo mount`)
  is mocked at the `_run_systemd_op_with_retry` subprocess boundary, since
  no real sudo/fstab-write is available or desirable in CI -- the same
  boundary every sibling `_ensure_*` test in this codebase mocks.

Plain functions (not test classes) are used throughout to keep each test
unit small and independently readable.
"""

import inspect
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Pure helper: _upgrade_cow_storage_fstab_entry_to_nfsv3
# ---------------------------------------------------------------------------


def test_upgrade_rewrites_nfs4_entry_to_nfsv3_nolock() -> None:
    content = (
        "192.168.60.23:/home/jsbattig/cow-storage /mnt/cow-storage nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\n"
    )
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is not None
    assert "/mnt/cow-storage nfs " in result, f"fstype must become nfs: {result!r}"
    assert "vers=3" in result
    assert "nolock" in result
    assert "192.168.60.23:/home/jsbattig/cow-storage" in result
    assert "soft" in result and "timeo=30" in result and "retrans=3" in result


def test_upgrade_returns_none_when_already_nfsv3_nolock() -> None:
    content = (
        "192.168.60.23:/home/jsbattig/cow-storage /mnt/cow-storage nfs "
        "_netdev,vers=3,nolock,soft,timeo=30,retrans=3 0 0\n"
    )
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is None


def test_upgrade_rewrites_when_nolock_present_but_still_nfs4() -> None:
    """The original #1510 fix added nolock alone without changing fstype
    or adding vers=3 -- that state must still be upgraded (nolock has no
    effect on NFSv4)."""
    content = (
        "192.168.60.23:/home/jsbattig/cow-storage /mnt/cow-storage nfs4 "
        "_netdev,soft,timeo=30,retrans=3,nolock 0 0\n"
    )
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is not None
    assert "/mnt/cow-storage nfs " in result
    assert "vers=3" in result


def test_upgrade_replaces_existing_vers_option() -> None:
    content = (
        "192.168.60.23:/home/jsbattig/cow-storage /mnt/cow-storage nfs4 "
        "_netdev,vers=4.1,soft,timeo=30,retrans=3 0 0\n"
    )
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is not None
    assert "vers=4.1" not in result
    assert "vers=3" in result
    options_line = [line for line in result.splitlines() if "/mnt/cow-storage" in line][
        0
    ]
    assert options_line.split()[3].count("vers=") == 1


def test_upgrade_returns_none_when_no_matching_entry() -> None:
    content = "/dev/sda1 / ext4 defaults 0 1\n"
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is None


def test_upgrade_preserves_unrelated_lines_and_comments() -> None:
    content = (
        "# /etc/fstab: static file system information.\n"
        "/dev/sda1 / ext4 defaults 0 1\n"
        "192.168.60.23:/home/jsbattig/cow-storage /mnt/cow-storage nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\n"
        "/swapfile none swap sw 0 0\n"
    )
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is not None
    lines = result.splitlines()
    assert lines[0] == "# /etc/fstab: static file system information."
    assert lines[1] == "/dev/sda1 / ext4 defaults 0 1"
    assert lines[3] == "/swapfile none swap sw 0 0"
    assert "nolock" in lines[2]
    assert "vers=3" in lines[2]


def test_upgrade_does_not_false_match_substring_mount_point() -> None:
    """A pre-existing /mnt/cow-storage-2 entry must never be mistaken for
    /mnt/cow-storage (mirrors the installer's own substring-match guard)."""
    content = (
        "192.168.60.23:/other /mnt/cow-storage-2 nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\n"
    )
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is None


def test_upgrade_preserves_leading_whitespace_and_line_ending() -> None:
    content = (
        "  192.168.60.23:/home/jsbattig/cow-storage /mnt/cow-storage nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\r\n"
    )
    result = DeploymentExecutor._upgrade_cow_storage_fstab_entry_to_nfsv3(
        content, "/mnt/cow-storage"
    )
    assert result is not None
    assert result.startswith("  192.168.60.23:")
    assert result.endswith("0 0\r\n")


# ---------------------------------------------------------------------------
# Orchestration method: _ensure_cow_storage_mount_options
# ---------------------------------------------------------------------------


def _make_cow_config(
    clone_backend: str = "cow-daemon",
    mount_point: Optional[str] = None,
    cow_daemon_none: bool = False,
) -> MagicMock:
    config = MagicMock()
    config.clone_backend = clone_backend
    if cow_daemon_none or mount_point is None:
        config.cow_daemon = None
    else:
        config.cow_daemon = MagicMock()
        config.cow_daemon.mount_point = mount_point
    return config


@pytest.fixture()
def executor() -> DeploymentExecutor:
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


def _run_step(executor: DeploymentExecutor, config: MagicMock, fstab_path: str) -> bool:
    with patch(
        "code_indexer.server.utils.config_manager.ServerConfigManager"
    ) as MockCM:
        MockCM.return_value.load_config.return_value = config
        return bool(executor._ensure_cow_storage_mount_options(fstab_path=fstab_path))


def test_noop_when_clone_backend_local(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text("/dev/sda1 / ext4 defaults 0 1\n")
    config = _make_cow_config(clone_backend="local")

    with patch.object(executor, "_run_systemd_op_with_retry") as mock_run:
        result = _run_step(executor, config, str(fstab))

    assert result is True
    mock_run.assert_not_called()


def test_noop_when_mount_point_empty(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text("/dev/sda1 / ext4 defaults 0 1\n")
    config = _make_cow_config(cow_daemon_none=True)

    with patch.object(executor, "_run_systemd_op_with_retry") as mock_run:
        result = _run_step(executor, config, str(fstab))

    assert result is True
    mock_run.assert_not_called()


def test_noop_when_fstab_already_nfsv3_nolock(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    mount_point = str(tmp_path / "cow-storage")
    fstab = tmp_path / "fstab"
    fstab.write_text(
        f"192.168.60.23:/home/jsbattig/cow-storage {mount_point} nfs "
        "_netdev,vers=3,nolock,soft,timeo=30,retrans=3 0 0\n"
    )
    config = _make_cow_config(mount_point=mount_point)

    with patch.object(executor, "_run_systemd_op_with_retry") as mock_run:
        result = _run_step(executor, config, str(fstab))

    assert result is True
    mock_run.assert_not_called()


def test_rewrites_fstab_and_attempts_umount_mount_cycle(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    mount_point = str(tmp_path / "cow-storage")
    fstab = tmp_path / "fstab"
    fstab.write_text(
        f"192.168.60.23:/home/jsbattig/cow-storage {mount_point} nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\n"
    )
    config = _make_cow_config(mount_point=mount_point)

    tee_result = subprocess.CompletedProcess(
        args=["sudo", "tee", str(fstab)], returncode=0, stdout="", stderr=""
    )
    umount_result = subprocess.CompletedProcess(
        args=["sudo", "umount", mount_point], returncode=0, stdout="", stderr=""
    )
    mount_result = subprocess.CompletedProcess(
        args=["sudo", "mount", mount_point], returncode=0, stdout="", stderr=""
    )

    with patch.object(
        executor,
        "_run_systemd_op_with_retry",
        side_effect=[tee_result, umount_result, mount_result],
    ) as mock_run:
        result = _run_step(executor, config, str(fstab))

    assert result is True
    assert mock_run.call_count == 3
    tee_call = mock_run.call_args_list[0]
    assert tee_call.args[0][:2] == ["sudo", "tee"]
    assert "vers=3" in tee_call.kwargs["input"]
    assert "nolock" in tee_call.kwargs["input"]
    umount_call = mock_run.call_args_list[1]
    assert umount_call.args[0] == ["sudo", "umount", mount_point]
    mount_call = mock_run.call_args_list[2]
    assert mount_call.args[0] == ["sudo", "mount", mount_point]


def test_umount_failure_is_non_fatal_mount_left_untouched(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    """fstab write succeeds but the live umount fails (e.g. EBUSY) ->
    still returns True (fstab is durably corrected; the mount itself is
    never touched further, and a future reboot will apply it)."""
    mount_point = str(tmp_path / "cow-storage")
    fstab = tmp_path / "fstab"
    fstab.write_text(
        f"192.168.60.23:/home/jsbattig/cow-storage {mount_point} nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\n"
    )
    config = _make_cow_config(mount_point=mount_point)

    tee_result = subprocess.CompletedProcess(
        args=["sudo", "tee", str(fstab)], returncode=0, stdout="", stderr=""
    )
    umount_failure = subprocess.CompletedProcess(
        args=["sudo", "umount", mount_point],
        returncode=1,
        stdout="",
        stderr="umount: target is busy",
    )

    with patch.object(
        executor,
        "_run_systemd_op_with_retry",
        side_effect=[tee_result, umount_failure],
    ) as mock_run:
        result = _run_step(executor, config, str(fstab))

    assert result is True
    # Follow-up mount must never be attempted after a failed umount.
    assert mock_run.call_count == 2


def test_mount_failure_after_successful_umount_returns_false(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    """fstab write and umount succeed but the follow-up mount fails ->
    genuine failure (mount point left unmounted), returns False."""
    mount_point = str(tmp_path / "cow-storage")
    fstab = tmp_path / "fstab"
    fstab.write_text(
        f"192.168.60.23:/home/jsbattig/cow-storage {mount_point} nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\n"
    )
    config = _make_cow_config(mount_point=mount_point)

    tee_result = subprocess.CompletedProcess(
        args=["sudo", "tee", str(fstab)], returncode=0, stdout="", stderr=""
    )
    umount_result = subprocess.CompletedProcess(
        args=["sudo", "umount", mount_point], returncode=0, stdout="", stderr=""
    )
    mount_failure = subprocess.CompletedProcess(
        args=["sudo", "mount", mount_point],
        returncode=1,
        stdout="",
        stderr="mount: wrong fs type",
    )

    with patch.object(
        executor,
        "_run_systemd_op_with_retry",
        side_effect=[tee_result, umount_result, mount_failure],
    ):
        result = _run_step(executor, config, str(fstab))

    assert result is False


def test_tee_failure_returns_false(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    mount_point = str(tmp_path / "cow-storage")
    fstab = tmp_path / "fstab"
    fstab.write_text(
        f"192.168.60.23:/home/jsbattig/cow-storage {mount_point} nfs4 "
        "_netdev,soft,timeo=30,retrans=3 0 0\n"
    )
    config = _make_cow_config(mount_point=mount_point)

    tee_failure = subprocess.CompletedProcess(
        args=["sudo", "tee", str(fstab)],
        returncode=1,
        stdout="",
        stderr="permission denied",
    )

    with patch.object(executor, "_run_systemd_op_with_retry", return_value=tee_failure):
        result = _run_step(executor, config, str(fstab))

    assert result is False


def test_missing_fstab_file_returns_false(
    executor: DeploymentExecutor, tmp_path: Path
) -> None:
    nonexistent = tmp_path / "does-not-exist" / "fstab"
    config = _make_cow_config(mount_point=str(tmp_path / "cow-storage"))

    with patch.object(executor, "_run_systemd_op_with_retry") as mock_run:
        result = _run_step(executor, config, str(nonexistent))

    assert result is False
    mock_run.assert_not_called()


def test_execute_wires_ensure_cow_storage_mount_options() -> None:
    """Regression guard (Messi Rule #12 anti-orphan-code): the new
    self-heal method must actually be called from execute(), not just
    defined. Static source inspection avoids standing up the entire
    multi-step execute() sequence (git pull, pip install, restart, etc.)
    just to prove one call site exists."""
    source = inspect.getsource(DeploymentExecutor.execute)
    assert "_ensure_cow_storage_mount_options()" in source
