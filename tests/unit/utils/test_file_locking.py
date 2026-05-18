"""Tests for NFS-safe file locking utilities.

Tests cover:
- Normal flock works and returns False (local filesystem)
- EBADF triggers lockf fallback and returns True (NFS mount)
- Other OSError (EPERM) is re-raised, not swallowed
- EACCES is re-raised, not treated as NFS fallback
- LOCK_NB passes through to flock on success and to lockf on NFS fallback
- LOCK_SH passes through to flock on success and to lockf on NFS fallback
- Unlock with used_lockf=False calls flock(LOCK_UN)
- Unlock with used_lockf=True calls lockf(LOCK_UN)
- Real-file lock/unlock round-trip succeeds without errors
- Non-blocking lock on uncontested real file succeeds
"""

import errno
import fcntl
from unittest.mock import patch

import pytest

from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock


class TestNfsSafeFlock:
    """Tests for nfs_safe_flock()."""

    def test_normal_flock_succeeds_returns_false(self):
        """When flock() succeeds, returns False (lockf not used)."""
        with patch("code_indexer.utils.file_locking.fcntl") as mock_fcntl:
            mock_fcntl.flock.return_value = None
            mock_fcntl.LOCK_EX = fcntl.LOCK_EX

            result = nfs_safe_flock(3, fcntl.LOCK_EX)

            assert result is False
            mock_fcntl.flock.assert_called_once_with(3, fcntl.LOCK_EX)
            mock_fcntl.lockf.assert_not_called()

    def test_ebadf_triggers_lockf_fallback_returns_true(self):
        """When flock() raises EBADF (NFS), falls back to lockf() and returns True."""
        ebadf_error = OSError(errno.EBADF, "Bad file descriptor")

        with patch("code_indexer.utils.file_locking.fcntl") as mock_fcntl:
            mock_fcntl.flock.side_effect = ebadf_error
            mock_fcntl.lockf.return_value = None
            mock_fcntl.LOCK_EX = fcntl.LOCK_EX

            result = nfs_safe_flock(3, fcntl.LOCK_EX)

            assert result is True
            mock_fcntl.flock.assert_called_once_with(3, fcntl.LOCK_EX)
            mock_fcntl.lockf.assert_called_once_with(3, fcntl.LOCK_EX)

    @pytest.mark.parametrize("err_no", [errno.EPERM, errno.EACCES])
    def test_non_ebadf_oserror_is_reraised_and_lockf_not_called(self, err_no):
        """Non-EBADF OSErrors are re-raised and lockf is never called."""
        error = OSError(err_no, "some error")

        with patch("code_indexer.utils.file_locking.fcntl") as mock_fcntl:
            mock_fcntl.flock.side_effect = error
            mock_fcntl.LOCK_EX = fcntl.LOCK_EX

            with pytest.raises(OSError) as exc_info:
                nfs_safe_flock(3, fcntl.LOCK_EX)

            assert exc_info.value.errno == err_no
            mock_fcntl.lockf.assert_not_called()

    @pytest.mark.parametrize(
        "operation",
        [
            fcntl.LOCK_EX | fcntl.LOCK_NB,
            fcntl.LOCK_SH,
        ],
        ids=["LOCK_EX|LOCK_NB", "LOCK_SH"],
    )
    def test_operation_passes_through_to_flock_on_success(self, operation):
        """Lock operation flags pass through unchanged to flock() on success."""
        with patch("code_indexer.utils.file_locking.fcntl") as mock_fcntl:
            mock_fcntl.flock.return_value = None
            # Expose all flag constants needed
            mock_fcntl.LOCK_EX = fcntl.LOCK_EX
            mock_fcntl.LOCK_NB = fcntl.LOCK_NB
            mock_fcntl.LOCK_SH = fcntl.LOCK_SH

            result = nfs_safe_flock(3, operation)

            assert result is False
            mock_fcntl.flock.assert_called_once_with(3, operation)

    @pytest.mark.parametrize(
        "operation",
        [
            fcntl.LOCK_EX | fcntl.LOCK_NB,
            fcntl.LOCK_SH,
        ],
        ids=["LOCK_EX|LOCK_NB", "LOCK_SH"],
    )
    def test_operation_passes_through_to_lockf_on_nfs_fallback(self, operation):
        """Lock operation flags pass through unchanged to lockf() on NFS fallback."""
        ebadf_error = OSError(errno.EBADF, "Bad file descriptor")

        with patch("code_indexer.utils.file_locking.fcntl") as mock_fcntl:
            mock_fcntl.flock.side_effect = ebadf_error
            mock_fcntl.lockf.return_value = None
            mock_fcntl.LOCK_EX = fcntl.LOCK_EX
            mock_fcntl.LOCK_NB = fcntl.LOCK_NB
            mock_fcntl.LOCK_SH = fcntl.LOCK_SH

            result = nfs_safe_flock(3, operation)

            assert result is True
            mock_fcntl.lockf.assert_called_once_with(3, operation)


class TestNfsSafeFunlock:
    """Tests for nfs_safe_funlock()."""

    def test_unlock_with_used_lockf_false_calls_flock_lock_un(self):
        """When used_lockf=False, unlocks via flock(LOCK_UN)."""
        with patch("code_indexer.utils.file_locking.fcntl") as mock_fcntl:
            mock_fcntl.LOCK_UN = fcntl.LOCK_UN

            nfs_safe_funlock(3, False)

            mock_fcntl.flock.assert_called_once_with(3, fcntl.LOCK_UN)
            mock_fcntl.lockf.assert_not_called()

    def test_unlock_with_used_lockf_true_calls_lockf_lock_un(self):
        """When used_lockf=True, unlocks via lockf(LOCK_UN)."""
        with patch("code_indexer.utils.file_locking.fcntl") as mock_fcntl:
            mock_fcntl.LOCK_UN = fcntl.LOCK_UN

            nfs_safe_funlock(3, True)

            mock_fcntl.lockf.assert_called_once_with(3, fcntl.LOCK_UN)
            mock_fcntl.flock.assert_not_called()


class TestNfsSafeFlockIntegration:
    """Integration tests using real files to verify lock/unlock round-trip."""

    def test_lock_unlock_roundtrip_real_file(self, tmp_path):
        """Lock and unlock a real temporary file without errors."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()

        with open(lock_file, "r+") as f:
            used_lockf = nfs_safe_flock(f.fileno(), fcntl.LOCK_EX)
            nfs_safe_funlock(f.fileno(), used_lockf)

    def test_nonblocking_on_uncontested_real_file_succeeds(self, tmp_path):
        """Non-blocking lock on a real uncontested file succeeds."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()

        with open(lock_file, "r+") as f:
            used_lockf = nfs_safe_flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            nfs_safe_funlock(f.fileno(), used_lockf)
