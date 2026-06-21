"""
Unit tests for Bug #1175: DeploymentLock crashes with PermissionError on Python 3.12.

Python 3.12 changed pathlib.Path.exists() to propagate PermissionError instead of
returning False. With systemd PrivateTmp=yes, the lock file path is inaccessible
(EACCES), causing crashes instead of graceful degradation.

Tests verify that acquire(), release(), and is_stale() return graceful values
when Path.exists() raises PermissionError.
"""

from pathlib import Path
from unittest.mock import patch

from code_indexer.server.auto_update.deployment_lock import DeploymentLock


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
