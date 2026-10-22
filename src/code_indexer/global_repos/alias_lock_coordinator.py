"""AliasLockCoordinator: drop-in WriteLockManager replacement dispatching
to the DB-backed alias lock.

Installed as ``RefreshScheduler.write_lock_manager`` in place of a bare
``WriteLockManager`` (wiring: refresh_scheduler.py's __init__). Every call
site reaches the lock through ``scheduler.acquire_write_lock()`` /
``release_write_lock()`` / ``is_write_locked()`` or
``scheduler.write_lock_manager.acquire/release/renew`` directly, so this one
instance rewires all of them with no call-site changes.

The DB lock is a committed, row-owned lease (``alias_lock_store/base.py``).
Release and renew act on the row ONLY with its exact owner token: the token
the caller passes, or the token of an acquisition THIS process made (kept in
``_acquired_tokens``, which only ever supplies this process's own token).
Owner names are shared across nodes ("refresh_scheduler" on every node), so
an owner name alone never proves ownership. A release served by another
process (write mode exited on a different worker) passes the token the
acquirer recorded in cluster-visible state. ``is_locked``/``get_lock_info``
read the store, never ``_acquired_tokens``. ``ttl_seconds`` is the lease,
exactly as for the file lock.

Dispatch:
    - acquire(): the rollout flag (``db_backed_enabled_getter``) picks the
      mechanism for a NEW lock. Flag on: refuse while a file lock exists,
      take the DB lock, then re-check the file lock and roll back on a
      conflict that appeared meanwhile. Flag off: refuse while the store
      reports a live DB holder, then take the file lock.
    - release()/renew(): the DB row when a store is wired and an exact token
      is known; otherwise (or when no such row exists) the file lock's own
      contract.
    - is_locked()/get_lock_info(): held under EITHER mechanism.

Cross-mechanism checks are check-then-act across two independent stores and
leave a narrow window during a flag flip; within one mechanism the primitive
(the row's primary key, or the file's O_CREAT|O_EXCL) serializes every
caller on every node.

``self._alias_locks`` (per-alias ``threading.Lock``) serializes this
process's own acquire() calls for one alias across the check-then-dispatch
sequence; it is process-local and gives no cross-process guarantee.

Typing note: ``AliasLockStore`` is imported only under ``TYPE_CHECKING`` so
this module (loaded in CLI/solo processes too) never pulls in the
server-only ``alias_lock_store`` package eagerly (keeps CLI imports light); runtime
imports from it are local.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Callable, DefaultDict, Dict, Optional, Tuple

from .write_lock_manager import DEFAULT_LOCK_TTL_SECONDS, WriteLockManager

if TYPE_CHECKING:
    from code_indexer.server.services.alias_lock_store.base import AliasLockStore

logger = logging.getLogger(__name__)


class AliasLockCoordinator:
    """Facade preserving WriteLockManager's bool-based public API
    (acquire/release/renew/is_locked/get_lock_info), dispatching between the
    legacy file-based lock and a DB-backed AliasLockStore."""

    def __init__(
        self,
        golden_repos_dir: Path,
        *,
        db_backed_enabled_getter: Optional[Callable[[], bool]] = None,
        store_resolver: Optional[Callable[[], "AliasLockStore"]] = None,
    ) -> None:
        """
        Args:
            golden_repos_dir: Forwarded to the wrapped WriteLockManager.
            db_backed_enabled_getter: CURRENT value of the rollout flag
                (AliasLockConfig.db_backed_enabled). `None` behaves as
                always-False (pure file-based mode). Consulted only in
                acquire().
            store_resolver: returns the AliasLockStore (production:
                ``alias_lock_store/factory.py``). `None` means no DB store.
        """
        self._file_manager = WriteLockManager(golden_repos_dir=golden_repos_dir)
        self._db_backed_enabled_getter = db_backed_enabled_getter or (lambda: False)
        self._store_resolver = store_resolver
        self._alias_locks: DefaultDict[str, threading.Lock] = defaultdict(
            threading.Lock
        )
        self._alias_locks_guard = threading.Lock()
        # ^ invariant only supplies THIS process's own acquisition token; never read for is_locked
        self._acquired_tokens: Dict[str, Tuple[str, str]] = {}
        self._acquired_tokens_lock = threading.Lock()

    def _db_backed(self) -> bool:
        return bool(self._db_backed_enabled_getter())

    def _get_alias_lock(self, alias: str) -> threading.Lock:
        with self._alias_locks_guard:
            return self._alias_locks[alias]

    def _token_for(
        self, alias: str, owner_name: str, owner_token: Optional[str]
    ) -> Optional[str]:
        """The exact token for (alias, owner_name): the caller's, else this
        process's own acquisition's, else None. An empty token is invalid."""
        if owner_token is not None:
            if not owner_token:
                raise ValueError("owner_token must be a non-empty string or None")
            return owner_token
        with self._acquired_tokens_lock:
            recorded = self._acquired_tokens.get(alias)
        if recorded is not None and recorded[0] == owner_name:
            return recorded[1]
        return None

    def _forget_token(self, alias: str, token: str) -> None:
        """Drop the remembered token once its lock is confirmed released or
        lost -- never before, so a failed release can be retried."""
        with self._acquired_tokens_lock:
            recorded = self._acquired_tokens.get(alias)
            if recorded is not None and recorded[1] == token:
                del self._acquired_tokens[alias]

    def acquire(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        owner_token: Optional[str] = None,
    ) -> bool:
        """Non-blocking acquire; see WriteLockManager.acquire(). In DB-backed
        mode `ttl_seconds` is the row's lease."""
        with self._get_alias_lock(alias):
            if self._db_backed():
                if self._file_manager.is_locked(alias):
                    return False
                return self._acquire_db_backed(
                    alias, owner_name, ttl_seconds, owner_token
                )
            if self._store_resolver is not None and self._store_resolver().is_held(
                alias
            ):
                return False
            return self._file_manager.acquire(
                alias,
                owner_name=owner_name,
                ttl_seconds=ttl_seconds,
                owner_token=owner_token,
            )

    def _acquire_db_backed(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int,
        owner_token: Optional[str],
    ) -> bool:
        """Take the DB lock, then re-check the file lock and roll back when a
        file lock appeared during the insert (see module docstring)."""
        assert self._store_resolver is not None, (
            "AliasLockCoordinator: db_backed_enabled_getter() returned True "
            "but no store_resolver was configured -- wiring bug"
        )
        store = self._store_resolver()
        handle = store.try_acquire(
            alias,
            operation=owner_name,
            owner_token=owner_token or str(uuid.uuid4()),
            ttl_seconds=ttl_seconds,
        )
        if handle is None:
            return False

        if self._file_manager.is_locked(alias):
            from code_indexer.server.services.alias_lock_store.base import (
                AliasLockOwnershipLostError,
            )

            try:
                store.release(handle)
                rollback_ok = True
            except AliasLockOwnershipLostError:
                rollback_ok = False
            logger.warning(
                f"AliasLockCoordinator._acquire_db_backed: detected "
                f"cross-mechanism conflict for alias={alias!r} (a file "
                f"lock appeared during the DB acquire) -- rolled back the "
                f"just-acquired DB lock (rollback_ok={rollback_ok})"
            )
            return False
        with self._acquired_tokens_lock:
            self._acquired_tokens[alias] = (owner_name, handle.owner_token)
        return True

    def release(
        self,
        alias: str,
        owner_name: str = "refresh_scheduler",
        owner_token: Optional[str] = None,
    ) -> bool:
        """Release `alias` held by `owner_name` under the exact token (the
        caller's, or this process's own acquisition's). False when the row
        belongs to another holder; True when released or not held
        (WriteLockManager.release()'s contract)."""
        token = self._token_for(alias, owner_name, owner_token)
        if self._store_resolver is not None and token is not None:
            from code_indexer.server.services.alias_lock_store.base import (
                LockOutcome,
            )

            outcome = self._store_resolver().release_owned(
                alias, owner_name, owner_token=token
            )
            self._forget_token(alias, token)
            if outcome is LockOutcome.RELEASED:
                return True
            if outcome is LockOutcome.OWNER_MISMATCH:
                logger.warning(
                    f"Write lock release refused for {alias!r}: caller="
                    f"{owner_name!r} does not own the DB-backed lock"
                )
                return False
        return self._file_manager.release(
            alias, owner_name=owner_name, owner_token=owner_token
        )

    def release_if_stale(
        self, alias: str, owner_name: str, owner_token: Optional[str] = None
    ) -> bool:
        """Release `alias` only while its holder is stale (lease lapsed or
        holder process gone), checked atomically with the release; True when
        released or nothing is held, False while a live holder remains."""
        token = self._token_for(alias, owner_name, owner_token)
        if self._store_resolver is not None and token is not None:
            from code_indexer.server.services.alias_lock_store.base import (
                LockOutcome,
            )

            outcome = self._store_resolver().release_if_stale(
                alias, owner_name, owner_token=token
            )
            if outcome is LockOutcome.RELEASED:
                self._forget_token(alias, token)
                return True
            if outcome is not LockOutcome.NOT_HELD:
                return False
        # ^ why is_locked evicts a stale lock file in the same call
        return not self._file_manager.is_locked(alias)

    def renew(
        self,
        alias: str,
        owner_name: str,
        ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
        owner_token: Optional[str] = None,
    ) -> bool:
        """Extend the lease of a lock held under the exact token (the
        caller's, or this process's own acquisition's).

        DB row present: True when renewed; RAISES AliasLockOwnershipLostError
        when the row belongs to someone else (callers such as the temporal
        migration heartbeat treat that as lock loss). Otherwise: the file
        lock's bool-returning renew()."""
        token = self._token_for(alias, owner_name, owner_token)
        if self._store_resolver is not None and token is not None:
            from code_indexer.server.services.alias_lock_store.base import (
                AliasLockOwnershipLostError,
                LockOutcome,
            )

            outcome = self._store_resolver().renew_owned(
                alias, owner_name, ttl_seconds=ttl_seconds, owner_token=token
            )
            if outcome is LockOutcome.RENEWED:
                return True
            if outcome in (LockOutcome.OWNER_MISMATCH, LockOutcome.LAPSED):
                self._forget_token(alias, token)
                raise AliasLockOwnershipLostError(
                    f"renew() for alias={alias!r} owner={owner_name!r}: the "
                    f"DB-backed lock is {outcome.value} -- ownership lost"
                )
        return self._file_manager.renew(
            alias,
            owner_name=owner_name,
            ttl_seconds=ttl_seconds,
            owner_token=owner_token,
        )

    def is_locked(self, alias: str) -> bool:
        """True if a live holder exists under EITHER mechanism."""
        if self._file_manager.is_locked(alias):
            return True
        if self._store_resolver is not None:
            return bool(self._store_resolver().is_held(alias))
        return False

    def get_lock_info(self, alias: str) -> Optional[Dict[str, object]]:
        """Live holder metadata (owner, pid, acquired_at, ...) under either
        mechanism, or None when not held."""
        if self._store_resolver is not None:
            holder = self._store_resolver().get_holder(alias)
            if holder is not None:
                return holder
        return self._file_manager.get_lock_info(alias)
