"""Primary-instance startup guard (Bug #1549).

Solo/SQLite job-orphan-cleanup sweeps (JobTracker.cleanup_orphaned_jobs_on_startup,
BackgroundJobManager._load_jobs_sqlite, BackgroundJobManager.fail_orphaned_jobs)
are intentionally UNSCOPED -- no per-row time or process-identity filter --
on the assumption that solo mode is always single-process, so any
running/pending row genuinely predates a real restart. That assumption
breaks whenever a second process runs the same startup sequence against
the same on-disk database while a live instance is already serving
traffic (proven live: a misconfigured systemd unit with Restart=always
retrying a port-bind failure ran the full startup sweep thousands of
times against an already-running out-of-band server, marking that live
instance's just-created jobs as "orphaned by restart" seconds after
creation).

acquire_primary_instance_lock() gives a server process a way to prove,
BEFORE running any destructive startup sweep, that no other process is
currently alive holding the same lock. The lock is a plain OS file lock
(flock/fcntl via `filelock`), intended to be held for the entire process
lifetime, and is released automatically by the kernel on process exit --
including a hard kill -- so a genuinely-dead previous instance never
blocks a real restart from acquiring it.

Finding 1 (Codex review, second pass): the original implementation used a
strictly NON-blocking acquire (timeout=0). initialize_services() runs
BEFORE uvicorn binds the port, so on a REAL restart where the outgoing
process has not fully exited yet -- slow lifespan teardown, or systemd's
Restart=always immediately relaunching the unit (this project's
installer configures RestartSec=10, see scripts/install-cidx-server.sh)
racing the predecessor's own shutdown -- the incoming, legitimate
process could fail to acquire and would skip BOTH orphan-cleanup sweeps.
Since the sweep only ever runs at startup and is never retried, that
left the previous instance's genuinely-orphaned rows stranded forever (a
`pending` row with no started_at permanently blocks the partial unique
active-job index for its (operation_type, repo_alias) pair). Fixed with
a BOUNDED blocking acquire: a genuinely-dead predecessor releases its
kernel-held lock within milliseconds of process exit, so a short bounded
wait comfortably covers restart overlap, while a genuinely-alive
duplicate (or a crash-looping process racing a real live instance -- the
scenario this module exists to protect) still holds the lock for the
entire wait and is correctly refused. The bound is deliberately short
relative to RestartSec=10 so a duplicate's wait never stacks into a
second systemd-visible restart delay.
"""

import logging
import math
import threading
from pathlib import Path
from typing import Dict

import filelock

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "primary_instance.lock"

# Finding 1: bounded wait for a same-restart predecessor to release the
# lock on its own exit. Chosen well under the installer's systemd
# RestartSec=10 (scripts/install-cidx-server.sh) so a genuine restart
# overlap resolves within a single restart cycle, while a duplicate
# process that is not exiting is still refused promptly rather than
# hanging startup indefinitely.
_DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 5.0

# Successfully-acquired locks are kept referenced here for the life of the
# process -- a filelock.FileLock releases its OS lock when garbage
# collected, so a lock that isn't kept alive somewhere would silently
# stop protecting anything. Guarded by _registry_lock since multiple
# threads could call acquire/release concurrently.
_held_locks: Dict[str, filelock.FileLock] = {}
_registry_lock = threading.Lock()


def _lock_path(server_data_dir: str) -> str:
    if not isinstance(server_data_dir, str) or not server_data_dir.strip():
        raise ValueError("server_data_dir must be a non-empty path")
    return str(Path(server_data_dir).resolve() / _LOCK_FILENAME)


def _validate_timeout(timeout: float) -> float:
    """Bug #1549 Finding 1: reject an invalid bound loudly rather than
    handing it to filelock, where a negative/NaN/infinite value could
    silently degrade back to non-blocking or genuinely-unbounded
    behavior."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError(
            f"timeout must be a finite non-negative number, got {timeout!r}"
        )
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError(
            f"timeout must be a finite non-negative number, got {timeout!r}"
        )
    return float(timeout)


def acquire_primary_instance_lock(
    server_data_dir: str, timeout: float = _DEFAULT_ACQUIRE_TIMEOUT_SECONDS
) -> bool:
    """Attempt to become the sole primary instance for server_data_dir.

    Bounded-blocking (Bug #1549 Finding 1): waits up to `timeout` seconds
    for the lock to become available before giving up. A genuinely-dead
    predecessor (including one exiting mid-restart) releases its
    kernel-held lock within milliseconds, so this reliably covers restart
    overlap; a genuinely-alive duplicate process still holds the lock for
    the entire wait and is correctly refused. Returns True if this
    process now holds exclusive ownership, False if another live process
    still holds it after the bound elapses.
    """
    timeout = _validate_timeout(timeout)
    path = _lock_path(server_data_dir)
    lock = filelock.FileLock(path, timeout=timeout)
    try:
        lock.acquire(timeout=timeout)
    except filelock.Timeout:
        logger.warning(
            "acquire_primary_instance_lock: another process still holds "
            "%s after waiting %.1fs -- this process is not the primary "
            "instance (Bug #1549)",
            path,
            timeout,
        )
        return False
    with _registry_lock:
        _held_locks[path] = lock
    return True


def release_primary_instance_lock(server_data_dir: str) -> None:
    """Release a previously-acquired lock. Production never calls this
    (the lock is held for the process's entire lifetime); it exists for
    test teardown and any future graceful-shutdown path."""
    path = _lock_path(server_data_dir)
    with _registry_lock:
        lock = _held_locks.pop(path, None)
    if lock is not None:
        lock.release()
