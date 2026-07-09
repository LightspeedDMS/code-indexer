"""NFS-safe file locking utilities.

fcntl.flock() uses BSD file locks which fail with EBADF on NFS mounts
configured with local_lock=none. This module provides wrappers that
try flock() first (better per-fd semantics on local filesystems) and
fall back to POSIX record locks via lockf() when flock() returns EBADF.

os.fsync() on directory file descriptors opened with O_RDONLY also raises
EBADF on NFS. nfs_safe_fsync() suppresses EBADF because NFS provides
close-to-open consistency — data is already at the server once the file is
closed, making the directory fsync redundant for durability.
"""

import errno
import fcntl
import logging
import os

logger = logging.getLogger(__name__)


def nfs_safe_fsync(fd: int) -> None:
    """Call os.fsync(fd), tolerating EBADF on NFS-mounted directories.

    On NFS mounts with local_lock=none, fsync() on a directory fd opened
    with O_RDONLY raises EBADF (errno 9). The NFS protocol's close-to-open
    consistency already guarantees data durability once the file is closed,
    so the directory fsync is redundant in that environment.

    Args:
        fd: File descriptor integer (from fileno() or os.open())

    Raises:
        OSError: Any OSError other than EBADF (e.g. EIO, ENOSPC)
    """
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno == errno.EBADF:
            logger.debug(
                "nfs_safe_fsync: ignoring EBADF on fd %d (NFS directory fsync)", fd
            )
            return
        raise


def nfs_safe_flock(fd: int, operation: int) -> bool:
    """Lock a file descriptor, NFS-safe.

    Tries flock() first. Falls back to lockf() on EBADF (NFS with
    local_lock=none, which does not support BSD file locks).

    Args:
        fd: File descriptor (from fileno())
        operation: Lock operation (fcntl.LOCK_EX, LOCK_SH, optionally | LOCK_NB)

    Returns:
        True if lockf was used (caller must pass this flag to nfs_safe_funlock)
        False if flock was used

    Raises:
        OSError: Any OSError other than EBADF (e.g. EPERM, EACCES, EWOULDBLOCK)
    """
    try:
        fcntl.flock(fd, operation)
        return False
    except OSError as e:
        if e.errno == errno.EBADF:
            fcntl.lockf(fd, operation)
            return True
        raise


def nfs_safe_funlock(fd: int, used_lockf: bool) -> None:
    """Unlock a file descriptor using the mechanism that was used to lock it.

    Must be called with the same used_lockf value returned by nfs_safe_flock
    on the same fd, so that the matching unlock primitive is used.

    Args:
        fd: File descriptor (from fileno())
        used_lockf: Value returned by nfs_safe_flock when the lock was acquired
    """
    if used_lockf:
        fcntl.lockf(fd, fcntl.LOCK_UN)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
