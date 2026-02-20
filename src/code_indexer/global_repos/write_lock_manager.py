"""
File-based named write locks for golden repo coordination (Story #230).

WriteLockManager provides externally-inspectable, process-restart-safe,
and stale-lock-evicting write locks stored as JSON files under
golden_repos_dir/.locks/{alias}.lock.

Lock file format:
    {
        "owner": "dependency_map_service",
        "pid": 12345,
        "acquired_at": "2026-02-19T10:00:00+00:00",
        "ttl_seconds": 3600
    }

Staleness rules (applied before any acquire or is_locked check):
    1. PID is dead  — os.kill(pid, 0) raises OSError(errno.ESRCH) → evict
    2. TTL expired  — acquired_at + ttl_seconds < now → evict
"""

import errno
import json
import logging
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class WriteLockManager:
    """
    File-based named write lock manager for golden repository coordination.

    Each alias gets its own lock file at:
        golden_repos_dir / ".locks" / f"{alias}.lock"

    Intra-process race protection uses a per-alias threading.Lock so that
    two threads in the same process cannot both open the same file with
    O_CREAT|O_EXCL simultaneously.

    Cross-process exclusion uses atomic O_CREAT|O_EXCL file creation.
    """

    def __init__(self, golden_repos_dir: Path) -> None:
        """
        Initialize WriteLockManager.

        Args:
            golden_repos_dir: Path to the golden repos root directory.
                              Lock files are stored under .locks/ within it.
        """
        self._golden_repos_dir = Path(golden_repos_dir)
        self._locks_dir = self._golden_repos_dir / ".locks"

        # Intra-process guards: one threading.Lock per alias
        # defaultdict so locks are created on first use without explicit initialisation
        self._intra_process_guards: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guards_lock = threading.Lock()  # protects _intra_process_guards dict

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, alias: str, owner_name: str, ttl_seconds: int = 3600) -> bool:
        """
        Non-blocking acquire of the write lock for the given alias.

        Steps:
            1. Ensure .locks directory exists.
            2. Acquire intra-process threading.Lock (keyed by alias).
            3. Check existing lock file for staleness (dead PID or TTL expired).
               If stale, delete it.
            4. Try atomic file creation with O_CREAT|O_EXCL.
            5. Write JSON metadata to the new file.
            6. Return True on success, False if lock already held.

        Args:
            alias: Repository alias without -global suffix (e.g., "cidx-meta").
            owner_name: Human-readable name for the lock owner.
            ttl_seconds: Lock TTL in seconds (default 3600 = 1 hour).

        Returns:
            True if lock was acquired, False if lock is already held.
        """
        self._locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_file(alias)

        intra_lock = self._get_intra_lock(alias)
        if not intra_lock.acquire(blocking=False):
            return False

        # The intra-process lock is only needed to protect the TOCTOU window between
        # checking the lock file and creating it atomically. Release it unconditionally
        # when we exit this block — the file itself is the durable guard.
        try:
            # Check if an existing lock file is stale; evict if so
            if lock_file.exists():
                if not self._evict_if_stale(lock_file):
                    # Lock file exists and is not stale — someone else holds it
                    return False

            # Atomic file creation: raises FileExistsError if file already exists
            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # Another process beat us to it between the staleness check and open
                return False

            # Write metadata
            try:
                metadata = {
                    "owner": owner_name,
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "ttl_seconds": ttl_seconds,
                }
                os.write(fd, json.dumps(metadata).encode())
            finally:
                os.close(fd)

            logger.debug(
                f"Write lock acquired: alias={alias!r} owner={owner_name!r} pid={os.getpid()}"
            )
            return True

        finally:
            # Always release intra-process lock; the file is the durable guard
            intra_lock.release()

    def release(self, alias: str, owner_name: str) -> bool:
        """
        Release the write lock for the given alias.

        Returns False (and logs a warning) if the lock is held by a different owner.
        Returns True (idempotent) if the lock file does not exist.

        Args:
            alias: Repository alias without -global suffix.
            owner_name: Must match the owner recorded in the lock file.

        Returns:
            True if lock was released or was not held, False if owner mismatch.
        """
        lock_file = self._lock_file(alias)

        if not lock_file.exists():
            return True

        try:
            content = json.loads(lock_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read lock file for {alias!r}: {e}")
            return True

        recorded_owner = content.get("owner", "")
        if recorded_owner != owner_name:
            logger.warning(
                f"Write lock release refused for {alias!r}: "
                f"caller={owner_name!r} but lock owned by {recorded_owner!r}"
            )
            return False

        try:
            lock_file.unlink()
        except FileNotFoundError:
            pass  # Already gone — idempotent
        except OSError as e:
            logger.warning(f"Could not delete lock file for {alias!r}: {e}")
            return False

        logger.debug(
            f"Write lock released: alias={alias!r} owner={owner_name!r}"
        )
        return True

    def is_locked(self, alias: str) -> bool:
        """
        Check whether the write lock for the given alias is currently held.

        If the lock file exists but is stale (dead PID or TTL expired), it is
        evicted and False is returned.

        Args:
            alias: Repository alias without -global suffix.

        Returns:
            True if a live lock exists, False otherwise.
        """
        lock_file = self._lock_file(alias)

        if not lock_file.exists():
            return False

        if self._evict_if_stale(lock_file):
            # File was stale and has been deleted
            return False

        return True

    def get_lock_info(self, alias: str) -> Optional[Dict]:
        """
        Return the lock metadata dict if a live lock exists, else None.

        Stale locks are evicted and None is returned.

        Args:
            alias: Repository alias without -global suffix.

        Returns:
            Dict with owner, pid, acquired_at, ttl_seconds — or None.
        """
        lock_file = self._lock_file(alias)

        if not lock_file.exists():
            return None

        try:
            content = json.loads(lock_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        # Check staleness
        if self._is_stale(content):
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        return content

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _lock_file(self, alias: str) -> Path:
        """Return the Path for the lock file of the given alias."""
        return self._locks_dir / f"{alias}.lock"

    def _get_intra_lock(self, alias: str) -> threading.Lock:
        """Get or create an intra-process threading.Lock for alias (thread-safe)."""
        with self._guards_lock:
            return self._intra_process_guards[alias]

    def _is_stale(self, content: Dict) -> bool:
        """
        Return True if the lock metadata indicates a stale lock.

        Stale if:
            - PID is dead (os.kill(pid, 0) raises OSError with errno.ESRCH), OR
            - acquired_at + ttl_seconds is in the past.
        """
        pid = content.get("pid")
        if pid is not None:
            try:
                os.kill(pid, 0)
            except OSError as e:
                if e.errno == errno.ESRCH:
                    return True
                # EPERM means process exists but we can't signal it — not stale
            except (TypeError, ValueError):
                pass

        acquired_at_str = content.get("acquired_at", "")
        ttl_seconds = content.get("ttl_seconds", 3600)
        if acquired_at_str:
            try:
                acquired_at = datetime.fromisoformat(acquired_at_str)
                now = datetime.now(timezone.utc)
                # Ensure acquired_at is timezone-aware for comparison
                if acquired_at.tzinfo is None:
                    acquired_at = acquired_at.replace(tzinfo=timezone.utc)
                elapsed = (now - acquired_at).total_seconds()
                if elapsed > ttl_seconds:
                    return True
            except (ValueError, TypeError):
                pass

        # If lock has neither PID nor timestamp, it cannot be validated — treat as stale
        if pid is None and not acquired_at_str:
            return True

        return False

    def _evict_if_stale(self, lock_file: Path) -> bool:
        """
        Read lock_file, check staleness, and delete if stale.

        Returns:
            True if the lock was stale and was deleted (or was already gone).
            False if the lock is live and should be respected.
        """
        try:
            content = json.loads(lock_file.read_text())
        except FileNotFoundError:
            return True  # Already gone
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Corrupt lock file {lock_file}: {e} — treating as stale")
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            return True

        if self._is_stale(content):
            logger.info(
                f"Evicting stale lock file {lock_file.name} "
                f"(owner={content.get('owner')!r}, pid={content.get('pid')})"
            )
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            return True

        return False
