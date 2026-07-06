"""Unit tests for code_indexer.utils.file_lock.exclusive_file_lock.

This single helper replaced the fcntl.flock pattern that was previously
copy-pasted across storage/server modules, so these tests are the one place
that guards the correctness rule for every call site -- most importantly the
regression guard that the lock file is opened WRITABLE (a read-only fd makes
LOCK_EX fail with EBADF over NFS).
"""

import builtins

import pytest

from code_indexer.utils.file_lock import exclusive_file_lock


def test_runs_body_and_creates_lock_file_with_parents(tmp_path):
    lock = tmp_path / "nested" / "dir" / "x.lock"  # parents do not exist yet
    ran = []
    with exclusive_file_lock(lock):
        ran.append(True)
    assert ran == [True]
    assert lock.exists()  # helper created the file and its parent directories


def test_opens_lock_file_writable_not_readonly(tmp_path, monkeypatch):
    """Regression guard for the NFS EBADF bug: the lock fd MUST be writable.

    fcntl.flock is advisory and ignores open mode on local filesystems, so this
    would silently pass with 'r' locally -- but over NFS a read-only fd makes
    LOCK_EX raise [Errno 9]. Assert on the open mode so a regression to 'r' is
    caught without needing an NFS mount.
    """
    lock = tmp_path / "x.lock"
    seen = {}
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if str(file) == str(lock):
            seen["mode"] = mode
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    with exclusive_file_lock(lock):
        pass

    assert seen.get("mode") is not None, "lock file was never opened"
    assert seen["mode"] != "r", "lock file opened read-only -> EBADF over NFS"
    assert any(c in seen["mode"] for c in ("a", "w", "+")), seen["mode"]


def test_nonblocking_raises_when_already_held(tmp_path):
    lock = tmp_path / "x.lock"
    with exclusive_file_lock(lock):
        # A second, independent open description cannot take the exclusive lock
        # without blocking -> BlockingIOError (subclass of OSError).
        with pytest.raises(OSError):
            with exclusive_file_lock(lock, blocking=False):
                pass


def test_lock_released_after_context(tmp_path):
    lock = tmp_path / "x.lock"
    with exclusive_file_lock(lock):
        pass
    # Previous holder exited, so a non-blocking acquire now succeeds.
    with exclusive_file_lock(lock, blocking=False):
        pass


def test_body_exception_still_releases_lock(tmp_path):
    lock = tmp_path / "x.lock"
    with pytest.raises(ValueError):
        with exclusive_file_lock(lock):
            raise ValueError("boom")
    # Lock must have been released despite the exception.
    with exclusive_file_lock(lock, blocking=False):
        pass
