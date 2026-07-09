"""
Unit tests for Bug #1175: DeploymentLock crashes with PermissionError on Python 3.12.

Python 3.12 changed pathlib.Path.exists() to propagate PermissionError instead of
returning False. With systemd PrivateTmp=yes, the lock file path is inaccessible
(EACCES), causing crashes instead of graceful degradation.

Tests verify that acquire(), release(), and is_stale() return graceful values
when Path.exists() raises PermissionError.

Extended tests verify the incomplete fix: the create-path in acquire() (the actual
open(lock_file, "w") write) also raises PermissionError under PrivateTmp=yes, and
must NOT re-raise — it must return True (proceed with deployment).

Also verifies the lock path is NOT under /tmp (PrivateTmp isolation).
"""

import builtins
from pathlib import Path
from unittest.mock import patch

from code_indexer.server.auto_update.deployment_lock import (
    DeploymentLock,
    get_default_lock_path,
)


class TestDeploymentLockPermissionError:
    """Tests that DeploymentLock handles PermissionError from Path.exists() gracefully."""

    def _make_lock(self, tmp_path: Path) -> DeploymentLock:
        return DeploymentLock(lock_file=tmp_path / "cidx-auto-update.lock")

    def test_acquire_does_not_raise_when_exists_raises_permission_error(
        self, tmp_path: Path
    ) -> None:
        """
        Bug #1175: acquire() must not propagate PermissionError from Path.exists().

        On Python 3.12 with systemd PrivateTmp=yes, Path.exists() raises PermissionError
        (EACCES) instead of returning False. _lock_file_exists() catches OSError and
        returns False, so acquire() treats it as "no existing lock" and proceeds to
        create the lock file, returning True (successfully acquired). The key invariant
        is that PermissionError is not propagated to the caller.
        """
        lock = self._make_lock(tmp_path)

        with patch.object(
            type(lock.lock_file),
            "exists",
            side_effect=PermissionError("EACCES: permission denied"),
        ):
            raised = None
            try:
                lock.acquire()
            except PermissionError as e:
                raised = e

        assert raised is None

    def test_release_does_not_raise_when_exists_raises_permission_error(
        self, tmp_path: Path
    ) -> None:
        """
        Bug #1175: release() must not propagate PermissionError from Path.exists().

        release() calls exists() to guard the unlink. If exists() raises PermissionError,
        release() must swallow it and return None gracefully.
        """
        lock = self._make_lock(tmp_path)

        with patch.object(
            type(lock.lock_file),
            "exists",
            side_effect=PermissionError("EACCES: permission denied"),
        ):
            result = lock.release()

        assert result is None

    def test_is_stale_returns_false_when_exists_raises_permission_error(
        self, tmp_path: Path
    ) -> None:
        """
        Bug #1175: is_stale() must not propagate PermissionError from Path.exists().

        is_stale() calls exists() to check whether the lock file is present. If exists()
        raises PermissionError, is_stale() must return False (cannot determine staleness).
        """
        lock = self._make_lock(tmp_path)

        with patch.object(
            type(lock.lock_file),
            "exists",
            side_effect=PermissionError("EACCES: permission denied"),
        ):
            result = lock.is_stale()

        assert result is False

    def test_acquire_returns_true_when_open_write_raises_permission_error(
        self, tmp_path: Path
    ) -> None:
        """
        Bug #1175 incomplete fix: acquire() create-path raises PermissionError.

        Under systemd PrivateTmp=yes, open(lock_file, "w") raises PermissionError
        (EACCES) because /tmp is per-service isolated. The original #1175 fix only
        hardened _lock_file_exists() (Path.exists() probe). The create-path
        (the actual file write in acquire()) still re-raised, permanently freezing
        the auto-update pipeline.

        acquire() must NOT re-raise when open(lock_file, "w") raises PermissionError.
        It must return True (proceed with deployment, accepting best-effort locking
        is better than an un-updatable node).
        """
        lock = self._make_lock(tmp_path)

        real_open = builtins.open

        def patched_open(file, mode="r", **kwargs):
            # Raise PermissionError only on the create-write to the lock file
            if str(file) == str(lock.lock_file) and "w" in str(mode):
                raise PermissionError("EACCES: permission denied on lock file write")
            return real_open(file, mode, **kwargs)

        # Make _lock_file_exists() return False (no existing lock) so acquire()
        # goes directly to the create-path.
        with (
            patch.object(type(lock.lock_file), "exists", return_value=False),
            patch("builtins.open", side_effect=patched_open),
        ):
            result = lock.acquire()

        assert result is True, (
            "acquire() must return True (proceed) when lock file write raises "
            "PermissionError — losing best-effort locking is better than freezing "
            "the deploy pipeline permanently"
        )

    def test_lock_path_is_not_under_tmp(self) -> None:
        """
        Bug #1175: default lock path must NOT be under /tmp.

        systemd PrivateTmp=yes isolates /tmp per-service. A lock file at
        /tmp/cidx-auto-update.lock raises PermissionError when the auto-updater
        (running as root) tries to access /tmp created for a different service.

        get_default_lock_path() must return a path anchored to CIDX_DATA_DIR
        (e.g. ~/.cidx-server/) which is shared between service units.
        """
        lock_path = get_default_lock_path()
        assert not str(lock_path).startswith("/tmp"), (
            f"Default lock path must NOT be under /tmp (PrivateTmp isolation). "
            f"Got: {lock_path}"
        )
