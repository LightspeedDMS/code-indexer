"""
Per-memory file lock for cidx-meta/memories/{memory_id}.md writes (Story #877).

Lock path layout (OUTSIDE any golden-repo clone):
    {locks_root}/cidx-meta/memories/{memory_id}.lock

Lock files live outside the base clone so they are never copied into versioned
snapshots or indexed.

Staleness rules (host-aware, mirrors WriteLockManager._is_stale):
    - hostname absent OR equals local host -> local lock:
        Stale if PID dead (os.kill raises ESRCH) OR TTL expired.
    - hostname present AND differs -> foreign lock:
        Stale ONLY if TTL expired. PID liveness never checked cross-node.
    - Backward compatibility: missing hostname treated as local.
    - Malformed or incomplete metadata: treated as live/unevictable.
      Staleness can only be asserted when evidence is present and valid.
"""

import errno
import json
import logging
import os
import socket
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Fallback TTL when lock file omits the ttl_seconds field.
DEFAULT_LOCK_TTL_SECONDS = 3600


def _validate_memory_id(memory_id: str) -> None:
    """Raise ValueError if memory_id could be used for path traversal."""
    if not memory_id:
        raise ValueError("memory_id must not be empty")
    if "/" in memory_id or "\\" in memory_id or "\x00" in memory_id:
        raise ValueError(
            f"Unsafe memory_id {memory_id!r}: must not contain '/', '\\\\', or NUL"
        )
    if ".." in memory_id:
        raise ValueError(f"Unsafe memory_id {memory_id!r}: must not contain '..'")


class MemoryFileLockManager:
    """
    Per-memory file lock for cidx-meta/memories/{memory_id}.md writes.

    Lock path layout (OUTSIDE any golden-repo clone):
        {locks_root}/cidx-meta/memories/{memory_id}.lock

    Intra-process race protection: one threading.Lock per memory_id.
    Cross-process exclusion: atomic O_CREAT|O_EXCL file creation.

    Host-aware staleness mirrors WriteLockManager._is_stale (Story #877).
    """

    def __init__(self, locks_root: Path) -> None:
        self._locks_dir = Path(locks_root) / "cidx-meta" / "memories"

        # One threading.Lock per memory_id, created on first use.
        self._intra_process_guards: Dict[str, threading.Lock] = defaultdict(
            threading.Lock
        )
        self._guards_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, memory_id: str, owner_name: str, ttl_seconds: int = 30) -> bool:
        """
        Non-blocking acquire. Returns True on success, False if another writer
        (live) holds the lock. Stale locks (dead local PID or TTL-expired) are
        evicted before acquisition.

        Raises ValueError for unsafe memory_id or non-positive ttl_seconds.
        """
        _validate_memory_id(memory_id)
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")

        self._locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self._lock_file(memory_id)

        intra_lock = self._get_intra_lock(memory_id)
        if not intra_lock.acquire(blocking=False):
            return False

        try:
            if lock_file.exists():
                if not self._evict_if_stale(lock_file):
                    return False

            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return False

            try:
                metadata: Dict[str, Any] = {
                    "owner": owner_name,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "ttl_seconds": ttl_seconds,
                }
                os.write(fd, json.dumps(metadata).encode())
            finally:
                os.close(fd)

            logger.debug(
                f"Memory lock acquired: memory_id={memory_id!r} owner={owner_name!r}"
            )
            return True

        finally:
            intra_lock.release()

    def release(self, memory_id: str, owner_name: str) -> bool:
        """
        Release. Idempotent when lock file does not exist. Returns False when
        owner does not match recorded owner.
        """
        _validate_memory_id(memory_id)
        lock_file = self._lock_file(memory_id)

        if not lock_file.exists():
            return True

        try:
            content: Dict[str, Any] = json.loads(lock_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Could not read lock file for {memory_id!r}: {exc}")
            return True

        recorded_owner = content.get("owner", "")
        if recorded_owner != owner_name:
            logger.warning(
                f"Memory lock release refused for {memory_id!r}: "
                f"caller={owner_name!r} but lock owned by {recorded_owner!r}"
            )
            return False

        try:
            lock_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(f"Could not delete lock file for {memory_id!r}: {exc}")
            return False

        logger.debug(
            f"Memory lock released: memory_id={memory_id!r} owner={owner_name!r}"
        )
        return True

    def is_locked(self, memory_id: str) -> bool:
        """Return True if a live (non-stale) lock exists for memory_id."""
        _validate_memory_id(memory_id)
        lock_file = self._lock_file(memory_id)

        if not lock_file.exists():
            return False

        if self._evict_if_stale(lock_file):
            return False

        return True

    def get_lock_info(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Return lock metadata dict if a live lock exists, else None."""
        _validate_memory_id(memory_id)
        lock_file = self._lock_file(memory_id)

        if not lock_file.exists():
            return None

        try:
            content: Dict[str, Any] = json.loads(lock_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Could not read lock file for {memory_id!r}: {exc}")
            return None

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

    def _lock_file(self, memory_id: str) -> Path:
        return self._locks_dir / f"{memory_id}.lock"

    def _get_intra_lock(self, memory_id: str) -> threading.Lock:
        with self._guards_lock:
            return self._intra_process_guards[memory_id]

    def _is_stale(self, content: Dict[str, Any]) -> bool:
        """
        Host-aware staleness check (mirrors WriteLockManager._is_stale).

        Local lock (hostname absent or matches local):
            Stale if PID dead OR TTL expired.
        Foreign lock (hostname differs):
            Stale ONLY if TTL expired.

        Incomplete or malformed metadata: return False (cannot prove staleness).
        Only return True when staleness can be positively established.
        """
        hostname = content.get("hostname")
        local_hostname = socket.gethostname()
        is_local = hostname is None or hostname == local_hostname

        raw_pid = content.get("pid")
        pid: Optional[int] = None
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pass  # non-numeric pid — skip liveness check

        if is_local and pid is not None:
            try:
                os.kill(pid, 0)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return True
                # EPERM: process exists, no permission — not stale

        acquired_at_str = content.get("acquired_at", "")

        raw_ttl = content.get("ttl_seconds")
        ttl_seconds: int
        try:
            ttl_seconds = (
                int(raw_ttl) if raw_ttl is not None else DEFAULT_LOCK_TTL_SECONDS
            )
        except (TypeError, ValueError):
            ttl_seconds = DEFAULT_LOCK_TTL_SECONDS

        if acquired_at_str:
            try:
                acquired_at = datetime.fromisoformat(str(acquired_at_str))
                now = datetime.now(timezone.utc)
                if acquired_at.tzinfo is None:
                    acquired_at = acquired_at.replace(tzinfo=timezone.utc)
                if (now - acquired_at).total_seconds() > ttl_seconds:
                    return True
            except (ValueError, TypeError):
                pass  # malformed timestamp — treat as non-stale

        # Cannot prove staleness — treat as live
        return False

    def _evict_if_stale(self, lock_file: Path) -> bool:
        """
        Read lock_file, check staleness, delete if stale.

        Returns True if stale (and deleted or already gone).
        Returns False if live OR if the file cannot be parsed (unevictable —
        cannot prove staleness from invalid metadata).
        """
        try:
            content: Dict[str, Any] = json.loads(lock_file.read_text())
        except FileNotFoundError:
            return True
        except (json.JSONDecodeError, OSError) as exc:
            # Cannot parse — cannot prove staleness. Treat as live to avoid
            # incorrectly evicting a lock whose metadata we cannot read.
            logger.warning(
                f"Unreadable lock file {lock_file}: {exc} — treating as live"
            )
            return False

        if self._is_stale(content):
            logger.info(
                f"Evicting stale memory lock {lock_file.name} "
                f"(owner={content.get('owner')!r}, pid={content.get('pid')})"
            )
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass
            return True

        return False
