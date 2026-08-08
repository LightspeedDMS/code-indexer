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
import socket
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, cast

logger = logging.getLogger(__name__)

# Default TTL for locks that do not specify one (1 hour).
# Used as fallback when "ttl_seconds" key is absent from lock metadata.
DEFAULT_LOCK_TTL_SECONDS = 3600


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
        self._intra_process_guards: Dict[str, threading.Lock] = defaultdict(
            threading.Lock
        )
        self._guards_lock = threading.Lock()  # protects _intra_process_guards dict

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int = 3600,
        owner_token: Optional[str] = None,
    ) -> bool:
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
            owner_token: Issue #1548 round-8 fix -- an OPTIONAL unique
                per-acquisition identity (e.g. a uuid4 hex string) recorded
                alongside ``owner_name``. ``owner_name`` alone is NOT a
                unique ownership token: two callers can legitimately share
                the same owner_name (e.g. two migration passes), so a
                stale holder whose lock already expired and was
                re-acquired by a fresh holder could otherwise still pass
                the owner_name check on ``renew()``/``release()``. When
                provided, callers MUST pass the SAME token to every
                subsequent ``renew()``/``release()`` call for this
                acquisition -- a mismatch is treated exactly like "this is
                no longer your lock". `None` (the default) preserves the
                pre-#1548-round-8 behavior of every existing caller that
                does not pass this argument.

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
                metadata: Dict[str, Any] = {
                    "owner": owner_name,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "ttl_seconds": ttl_seconds,
                }
                if owner_token is not None:
                    metadata["owner_token"] = owner_token
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

    def release(
        self,
        alias: str,
        owner_name: str,
        owner_token: Optional[str] = None,
    ) -> bool:
        """
        Release the write lock for the given alias.

        Returns False (and logs a warning) if the lock is held by a different owner.
        Returns True (idempotent) if the lock file does not exist.

        Args:
            alias: Repository alias without -global suffix.
            owner_name: Must match the owner recorded in the lock file.
            owner_token: Issue #1548 round-8 fix -- when provided, must
                match the ``owner_token`` recorded on the lock file by
                ``acquire()`` (see ``acquire()``'s docstring for why
                ``owner_name`` alone is not a unique ownership token). A
                mismatch is refused exactly like an ``owner_name``
                mismatch -- this is no longer the caller's lock to
                release. `None` (the default) skips this check entirely,
                preserving the pre-#1548-round-8 behavior of every
                existing caller that does not pass this argument.

        Returns:
            True if lock was released or was not held, False if owner
            (or owner_token) mismatch.
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

        if owner_token is not None:
            recorded_token = content.get("owner_token")
            if recorded_token != owner_token:
                logger.warning(
                    f"Write lock release refused for {alias!r}: owner_token "
                    f"mismatch -- this lock may have been released and "
                    f"re-acquired by a different holder since the caller "
                    f"last observed it"
                )
                return False

        try:
            lock_file.unlink()
        except FileNotFoundError:
            pass  # Already gone — idempotent
        except OSError as e:
            logger.warning(f"Could not delete lock file for {alias!r}: {e}")
            return False

        logger.debug(f"Write lock released: alias={alias!r} owner={owner_name!r}")
        return True

    def renew(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        owner_token: Optional[str] = None,
    ) -> bool:
        """
        Extend the lease of a lock currently held by ``owner_name``.

        Issue #1548 round-7 fix: gives a legitimately long-running holder
        (e.g. temporal-legacy-migration) a heartbeat mechanism instead of
        depending solely on a long-but-finite TTL. See ``_renew_locked``
        for the actual read-validate-write sequence, protected by the same
        per-alias intra-process lock ``acquire()`` uses -- the CROSS-process
        race against another process's ``acquire()``/``release()`` is an
        accepted, pre-existing residual of this lock's file-based design
        (same as ``release()``), not newly introduced here.

        Issue #1548 round-8 fix: ``owner_token``, when provided, must match
        the token recorded on the lock file by ``acquire()`` -- see
        ``acquire()``'s docstring for why ``owner_name`` alone is not a
        unique ownership token (a stale holder whose TTL already expired
        and was re-acquired by a fresh holder under the SAME owner_name
        would otherwise still pass the owner_name check here). This is
        re-checked a SECOND time, against a freshly re-read lock file,
        immediately before the actual write in
        ``_write_renewed_lock_content`` -- closing the narrower race where
        the lock is released or re-acquired by a different holder WHILE
        this renewal was busy building its temp file.

        Returns:
            True if the lease was renewed, False on a missing lock file,
            an owner or owner_token mismatch, unreadable content, or a
            lock state that changed immediately before the write.

        Raises:
            ValueError: alias/owner_name is blank, or ttl_seconds is not a
                positive integer.
        """
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError("alias must be a non-blank string")
        if not isinstance(owner_name, str) or not owner_name.strip():
            raise ValueError("owner_name must be a non-blank string")
        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")

        with self._get_intra_lock(alias):
            return self._renew_locked(alias, owner_name, ttl_seconds, owner_token)

    def _renew_locked(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int,
        owner_token: Optional[str] = None,
    ) -> bool:
        """Read-validate-write body of ``renew()``, called under the
        per-alias intra-process lock. Split out to keep ``renew()`` short.
        """
        lock_file = self._lock_file(alias)
        if not lock_file.exists():
            logger.warning(
                f"Cannot renew write lock for {alias!r}: no lock file present"
            )
            return False

        try:
            content = json.loads(lock_file.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Cannot renew write lock for {alias!r}: {e}")
            return False
        if not isinstance(content, dict):
            logger.warning(
                f"Cannot renew write lock for {alias!r}: lock file "
                f"content is not a JSON object"
            )
            return False

        recorded_owner = content.get("owner")
        if not recorded_owner or recorded_owner != owner_name:
            logger.warning(
                f"Write lock renewal refused for {alias!r}: "
                f"caller={owner_name!r} but lock owned by {recorded_owner!r}"
            )
            return False

        if owner_token is not None:
            recorded_token = content.get("owner_token")
            if recorded_token != owner_token:
                logger.warning(
                    f"Write lock renewal refused for {alias!r}: owner_token "
                    f"mismatch -- this lock may have been released and "
                    f"re-acquired by a different holder (owner_name alone "
                    f"is not a unique ownership token)"
                )
                return False

        content["acquired_at"] = datetime.now(timezone.utc).isoformat()
        content["ttl_seconds"] = ttl_seconds
        return self._write_renewed_lock_content(
            alias, lock_file, content, owner_name=owner_name, owner_token=owner_token
        )

    def _current_lock_state_matches(
        self,
        lock_file: Path,
        owner_name: str,
        owner_token: Optional[str],
    ) -> bool:
        """Fresh, unconditional re-read of *lock_file*'s CURRENT on-disk
        state -- True iff it still exists, is a JSON object, and its
        recorded owner (and owner_token, when *owner_token* is not None)
        still match. Used ONLY as the immediate pre-write recheck inside
        ``_write_renewed_lock_content`` (Issue #1548 round-8 Issue 3) --
        deliberately independent of any value read earlier in the same
        renewal, since the whole point is to detect a change that
        happened IN BETWEEN.
        """
        if not lock_file.exists():
            return False
        try:
            content = json.loads(lock_file.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        if not isinstance(content, dict):
            return False
        if content.get("owner") != owner_name:
            return False
        if owner_token is not None and content.get("owner_token") != owner_token:
            return False
        return True

    def _write_renewed_lock_content(
        self,
        alias: str,
        lock_file: Path,
        content: Dict,
        *,
        owner_name: str,
        owner_token: Optional[str] = None,
    ) -> bool:
        """Atomically persist *content* over *lock_file* (uuid4-suffixed
        temp file + ``os.replace`` -- never a bare ``write_text()``,
        matching this codebase's other durable-metadata-write conventions
        and collision-free across concurrent renewals of DIFFERENT
        aliases).

        Issue #1548 round-8 fix (Issue 3): immediately before the atomic
        ``os.replace`` -- as close to the actual write as achievable --
        the lock's CURRENT on-disk owner/owner_token is re-verified via
        ``_current_lock_state_matches``. A renewal that was slow/blocked
        building the temp file above, during which the lock was released
        (file deleted) or re-acquired by a different holder (owner_token
        changed), MUST refuse to write here rather than blindly
        recreating/renewing a lock it no longer legitimately holds --
        ``os.replace`` onto a since-deleted path would otherwise silently
        RECREATE the lock file post-release.
        """
        tmp_path = lock_file.with_name(f"{lock_file.name}.tmp-{uuid.uuid4().hex}")
        try:
            tmp_path.write_text(json.dumps(content))
            if not self._current_lock_state_matches(lock_file, owner_name, owner_token):
                logger.warning(
                    f"Write lock renewal aborted for {alias!r} immediately "
                    f"before write -- lock state changed (released or "
                    f"re-acquired by a different holder) since this "
                    f"renewal began"
                )
                return False
            os.replace(tmp_path, lock_file)
        except OSError as e:
            logger.warning(f"Could not renew lock file for {alias!r}: {e}")
            return False
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        logger.debug(
            f"Write lock renewed: alias={alias!r} owner={content.get('owner')!r}"
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

        return cast(Optional[dict[Any, Any]], content)

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

        Host-aware staleness rules (Story #877):
            - If hostname field is absent OR matches local host -> local lock:
                Stale if PID is dead (os.kill(pid, 0) raises OSError with errno.ESRCH)
                OR acquired_at + ttl_seconds is in the past.
            - If hostname field differs from local host -> foreign lock:
                Stale ONLY if acquired_at + ttl_seconds is in the past.
                PID liveness is NEVER checked for foreign hosts (meaningless cross-node).

        Backward compatibility: locks without a hostname field are treated as local.
        """
        hostname = content.get("hostname")
        local_hostname = socket.gethostname()
        is_local = hostname is None or hostname == local_hostname

        pid = content.get("pid")
        if is_local and pid is not None:
            try:
                os.kill(pid, 0)
            except OSError as e:
                if e.errno == errno.ESRCH:
                    return True
                # EPERM: process exists but we lack permission to signal it — not stale
            except (TypeError, ValueError):
                # pid field is present but not a valid integer — cannot check liveness;
                # fall through to TTL check rather than incorrectly evicting the lock
                pass

        acquired_at_str = content.get("acquired_at", "")

        # Validate ttl_seconds explicitly; non-numeric values fall back to default
        raw_ttl = content.get("ttl_seconds")
        try:
            ttl_seconds = (
                int(raw_ttl) if raw_ttl is not None else DEFAULT_LOCK_TTL_SECONDS
            )
        except (TypeError, ValueError):
            ttl_seconds = DEFAULT_LOCK_TTL_SECONDS

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
                # Malformed acquired_at timestamp cannot be parsed;
                # treat the lock as non-stale rather than incorrectly evicting it
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
