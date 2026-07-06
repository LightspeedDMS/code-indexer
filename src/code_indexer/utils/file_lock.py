"""Cross-process exclusive file locking.

Single home for the ``fcntl.flock`` pattern that was previously copy-pasted
across storage and server modules. Centralizes one correctness rule that is
easy to get wrong per-site: **the lock file must be opened for WRITE.**

``fcntl.flock`` is advisory and ignores the open mode on local filesystems, so
an exclusive lock taken on a read-only (``"r"``) fd works there. But over NFS
the lock becomes a real server-side operation and ``LOCK_EX`` on a read-only fd
is rejected with ``OSError: [Errno 9] Bad file descriptor``. Append mode is
writable, never truncates, and creates the file if missing, so ``LOCK_EX`` works
on both local and NFS-backed data directories.
"""

from __future__ import annotations

import contextlib
import fcntl
from pathlib import Path
from typing import Generator


@contextlib.contextmanager
def exclusive_file_lock(
    lock_path: Path, *, blocking: bool = True
) -> Generator[None, None, None]:
    """Hold an exclusive advisory (``LOCK_EX``) lock on ``lock_path``.

    The lock file's parent directory and the file itself are created if missing.
    The lock is released automatically when the context exits.

    Args:
        lock_path: Path to the lock file.
        blocking: When True (default) block until the lock is acquired. When
            False, raise ``BlockingIOError`` immediately if another holder
            already has it (``LOCK_NB``).

    Yields:
        None -- use as ``with exclusive_file_lock(path): ...``.

    Raises:
        BlockingIOError: if ``blocking=False`` and the lock is already held.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    # Open WRITE ("a"), not read: LOCK_EX on a read-only fd -> EBADF over NFS.
    with open(lock_path, "a") as lock_f:
        fcntl.flock(lock_f.fileno(), flags)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
