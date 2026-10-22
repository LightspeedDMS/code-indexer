"""Shared abstraction for the DB-backed golden-repo alias lock.

Architecture: the lock is a COMMITTED ROW with a lease, owned by the row's
identity -- never by the database session that inserted it.

    Row: ``lock_key`` (primary key), ``owner_token``, ``operation`` (the
    caller's owner name), ``hostname`` + ``pid`` of the acquiring process,
    ``ttl_seconds``, ``acquired_at``, ``last_renewed_at``.

    - Acquire: insert the row in a short transaction and commit. If a row
      already exists and its holder is STALE it is taken over atomically
      (row-locked re-read, then an exact-token UPDATE).
    - Staleness: ``write_lock_manager.lock_holder_is_stale`` -- the SAME rule
      the file-based ``WriteLockManager`` applies: the lease lapsed
      (database clock: ``now - last_renewed_at > ttl_seconds``), or the
      holder process on THIS host is gone. Default lease
      ``DEFAULT_LOCK_TTL_SECONDS``; callers guarding long work pass a longer
      ``ttl_seconds`` or renew, exactly as with the file lock.
    - Release/renew: by the EXACT owner token plus operation
      (``release_owned``/``renew_owned``), from whichever process holds the
      token -- e.g. a write mode exited on another worker, which reads the
      token the enter recorded. Owner names are shared across nodes, so a
      name alone never releases or renews anything.
    - Renew: extends the lease (``last_renewed_at = now``); never shortens
      the TTL recorded at acquire.

No connection or transaction is held between calls, so nothing pins the
PostgreSQL vacuum horizon and nothing depends on which worker serves the
next request.

Concrete backends: ``sqlite_store.SqliteAliasLockStore`` (solo, node-local
per-alias files), ``postgres_store.PostgresAliasLockStore`` (cluster).
"""

from __future__ import annotations

import enum
import functools
import os
import socket
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


@functools.lru_cache(maxsize=1)
def local_holder_identity() -> str:
    """Identity under which a holder's PID is meaningful: host name, kernel
    boot id and PID namespace. Two containers sharing a host name (or one
    host after a reboot) get different identities, so a PID is only ever
    checked inside the namespace that issued it. When the ids cannot be
    read, the identity is unique to this process: PID liveness is then never
    applied to its locks, which fall back to their lease alone."""
    hostname = socket.gethostname()
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            boot_id = handle.read().strip()
        pid_namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        return f"{hostname}|unidentified|{uuid.uuid4().hex}"
    return f"{hostname}|{boot_id}|{pid_namespace}"


class AliasLockOwnershipLostError(RuntimeError):
    """Raised when a handle-based release/renew finds the row no longer
    belongs to that handle (released elsewhere, or taken over after its
    lease lapsed)."""


class LockOutcome(enum.Enum):
    """Result of an owner-addressed release/renew."""

    RELEASED = "released"
    RENEWED = "renewed"
    NOT_HELD = "not_held"
    OWNER_MISMATCH = "owner_mismatch"
    LAPSED = "lapsed"  # the row matches but its holder is stale: not confirmed
    STILL_HELD = "still_held"  # a release-if-stale found a live holder


@dataclass
class AliasLockHandle:
    """An acquired alias lock.

    Carries the row identity (`lock_key`, `owner_token`, `operation`).
    `_connection` is kept for API compatibility and is always None: the row,
    not a session, is the lock.
    """

    lock_key: str
    owner_token: str
    operation: str
    _connection: Any = field(default=None, repr=False)
    _store: Optional["AliasLockStore"] = field(default=None, repr=False, compare=False)
    _released: bool = field(default=False, repr=False, compare=False)
    _release_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    def __enter__(self) -> "AliasLockHandle":
        """Entering an already-released handle raises: running the body would
        otherwise look locked while nothing is held."""
        with self._release_lock:
            if self._released:
                raise RuntimeError(
                    f"AliasLockHandle.__enter__ called for "
                    f"lock_key={self.lock_key!r} but this handle was already "
                    f"released -- entering `with handle:` on an "
                    f"already-released handle would silently run its body "
                    f"with NO LOCK HELD AT ALL."
                )
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Releases exactly once; a no-op when already released; raises when
        the handle was not built by a store (it could release nothing)."""
        with self._release_lock:
            if self._released:
                return
            if self._store is None:
                raise RuntimeError(
                    f"AliasLockHandle.__exit__ called for "
                    f"lock_key={self.lock_key!r} but no _store reference is "
                    f"set -- this handle was not constructed by "
                    f"AliasLockStore.try_acquire(), so `with handle:` cannot "
                    f"release anything. Construct handles only via "
                    f"try_acquire()."
                )
            self._store.release(self)


def release_handle(store: "AliasLockStore", handle: AliasLockHandle) -> None:
    """Shared handle-based release for both backends: exact-token release,
    at most once per handle; any outcome but RELEASED is ownership loss."""
    with handle._release_lock:
        if handle._released:
            raise AliasLockOwnershipLostError(
                f"release() called again for lock_key={handle.lock_key!r} "
                f"owner_token={handle.owner_token!r} -- already released"
            )
        handle._released = True
        outcome = store.release_owned(
            handle.lock_key, handle.operation, owner_token=handle.owner_token
        )
    if outcome is not LockOutcome.RELEASED:
        raise AliasLockOwnershipLostError(
            f"release() for lock_key={handle.lock_key!r} "
            f"owner_token={handle.owner_token!r} found {outcome.value} -- "
            f"ownership already lost"
        )


def renew_handle(
    store: "AliasLockStore", handle: AliasLockHandle, ttl_seconds: Optional[int]
) -> None:
    """Shared handle-based renew for both backends."""
    with handle._release_lock:
        if handle._released:
            raise AliasLockOwnershipLostError(
                f"renew() called for lock_key={handle.lock_key!r} "
                f"owner_token={handle.owner_token!r} but the lock was "
                f"already released -- ownership already lost"
            )
        outcome = store.renew_owned(
            handle.lock_key,
            handle.operation,
            ttl_seconds=ttl_seconds,
            owner_token=handle.owner_token,
        )
    if outcome is not LockOutcome.RENEWED:
        raise AliasLockOwnershipLostError(
            f"renew() for lock_key={handle.lock_key!r} "
            f"owner_token={handle.owner_token!r} found {outcome.value} -- "
            f"ownership already lost"
        )


class AliasLockStore(Protocol):
    """Protocol implemented by both the SQLite and PostgreSQL alias lock stores."""

    def try_acquire(
        self,
        lock_key: str,
        operation: str,
        owner_token: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[AliasLockHandle]:
        """Commit a lock row for `lock_key` (taking over a stale holder's
        row). Returns a handle, or None when a live holder exists.
        ``ttl_seconds=None`` means ``DEFAULT_LOCK_TTL_SECONDS``."""
        ...

    def release(self, handle: AliasLockHandle) -> None:
        """Exact-token release; raises AliasLockOwnershipLostError when the
        row no longer belongs to `handle`."""
        ...

    def renew(self, handle: AliasLockHandle, ttl_seconds: Optional[int] = None) -> None:
        """Exact-token lease extension; raises AliasLockOwnershipLostError
        when the row no longer belongs to `handle`."""
        ...

    def release_owned(
        self,
        lock_key: str,
        operation: str,
        *,
        owner_token: str,
        only_if_stale: bool = False,
    ) -> LockOutcome:
        """Delete the row when BOTH its operation and its exact owner_token
        match (an owner name alone never proves ownership). RELEASED,
        NOT_HELD (no row) or OWNER_MISMATCH; with only_if_stale, STILL_HELD
        when the holder is live (checked in the same transaction)."""
        ...

    def release_if_stale(
        self, lock_key: str, operation: str, *, owner_token: str
    ) -> LockOutcome:
        """release_owned(only_if_stale=True): delete the matching row only
        while its holder is stale (lease lapsed or holder process gone)."""
        ...

    def renew_owned(
        self,
        lock_key: str,
        operation: str,
        *,
        owner_token: str,
        ttl_seconds: Optional[int] = None,
    ) -> LockOutcome:
        """Restart the lease of the row with this operation AND exact
        owner_token. RENEWED, NOT_HELD or OWNER_MISMATCH."""
        ...

    def is_held(self, lock_key: str) -> bool:
        """True when a live (non-stale) holder exists. Bounded; never waits
        for a holder to finish."""
        ...

    def get_holder(self, lock_key: str) -> Optional[Dict[str, object]]:
        """Metadata of the live holder (owner, pid, hostname, acquired_at,
        ttl_seconds, db_backed), or None when not held or stale."""
        ...
