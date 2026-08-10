"""Shared abstraction for the DB-backed golden-repo alias lock (Issue #1546).

Architecture (accepted design, see the issue for full rationale):

    The lock IS a held database transaction, not a row with a TTL.

    - Acquire: ``INSERT ... ON CONFLICT (lock_key) DO NOTHING RETURNING ...``
      (PostgreSQL) / ``BEGIN IMMEDIATE; INSERT OR IGNORE ...; SELECT
      changes()`` (SQLite), executed on a DEDICATED connection that is held
      open -- with its transaction left uncommitted -- for the entire
      lifetime of the lock. A crash or connection death causes the
      underlying database to roll the transaction back automatically, so
      the lock is released immediately with no TTL and no heartbeat.
    - Release: an exact-token ``DELETE ... WHERE lock_key = ? AND
      owner_token = ?`` on that SAME connection/transaction. Zero rows
      affected means ownership was already lost (e.g. the connection died
      and something else raced in) -- this is reported loudly via
      ``AliasLockOwnershipLostError``, never silently swallowed.
    - Renew: an exact-token ``UPDATE ... SET last_renewed_at = now()``,
      diagnostic-only. It is NEVER used to decide ownership and carries no
      TTL semantics; the same zero-rows-means-ownership-lost contract
      applies so callers can use it as an early ownership-loss checkpoint
      during a long-running operation, without it granting any renewed
      lease.

    There is deliberately no reaper, no lease expiry, and no
    local-clock-vs-remote-mtime comparison anywhere: a committed row that
    outlives its holder's connection is impossible by construction (the
    transaction is never committed until release), so there is nothing to
    age out.

This module defines the shared vocabulary (`AliasLockHandle`, the ownership
loss exception, and the `AliasLockStore` protocol) both backends implement.
Concrete backends: `sqlite_store.SqliteAliasLockStore`,
`postgres_store.PostgresAliasLockStore`.

Post-review corrections (see the issue's Phase 1 rework for full detail):

    - PostgreSQL's acquire INSERT is bounded by an explicit
      ``lock_timeout`` (default 0.5s, configurable): without it, a
      contended acquire blocked for as long as the holder kept the lock
      (proven empirically to hang 8+ seconds) instead of returning
      `None` promptly. A `psycopg.errors.LockNotAvailable` from that
      bound is translated to a clean `None`.
    - SQLite uses one DEDICATED file PER ALIAS (never a single shared
      file): SQLite has no row-level locking, so a shared file made
      acquiring alias A falsely block/report contention on a
      completely unrelated alias B.
    - `renew()` on BOTH backends is diagnostic-only in the strictest
      sense: a wrong-token renew() raises `AliasLockOwnershipLostError`
      WITHOUT rolling back or closing the connection -- the real held
      transaction (whatever it is) stays open. Only `release()` can end
      the lock's lifecycle.
    - Both backends normalize a genuine "connection is actually dead"
      failure (not just an already-explicitly-closed connection) to
      `AliasLockOwnershipLostError` during `release()`/`renew()` --
      e.g. PostgreSQL's `psycopg.OperationalError` (a server-side
      `pg_terminate_backend` leaves `conn.closed` False locally during
      the failing call itself), or SQLite's `sqlite3.Error` (a severed
      underlying file descriptor).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


class AliasLockOwnershipLostError(RuntimeError):
    """Raised when a release/renew call discovers ownership was already lost.

    This happens when the exact-token DELETE/UPDATE affects zero rows --
    the definitive, atomic signal that either the row is gone (e.g. the
    holder's connection died and a caller is trying to use a handle whose
    underlying transaction the database already rolled back) or the token
    no longer matches (should be structurally impossible under correct
    usage, since only the holder ever knows its own token, but is
    detected and reported the same way rather than assumed away).
    """


@dataclass
class AliasLockHandle:
    """An acquired alias lock.

    Carries the identifying fields callers need (`lock_key`, `owner_token`,
    `operation`) plus an opaque backend-private connection object that MUST
    NOT be inspected or used directly by callers -- it exists solely so the
    owning `AliasLockStore` can operate on the exact connection/transaction
    that acquired the lock when `release()`/`renew()` are called.
    """

    lock_key: str
    owner_token: str
    operation: str
    _connection: Any = field(repr=False)
    _store: Optional["AliasLockStore"] = field(default=None, repr=False, compare=False)
    _released: bool = field(default=False, repr=False, compare=False)
    _release_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    def __enter__(self) -> "AliasLockHandle":
        """Escalated by round-3 review (F2's vacuum-pinning cost makes a
        leaked open transaction worse than an ordinary leaked
        connection): `with handle:` releases the lock on exit, so
        Phase 2's real call sites don't each need to hand-roll their
        own try/finally around acquire/release.

        Round-4 review fix (Finding #4): raises loudly if this handle
        was ALREADY released before entry -- without this check,
        `store.try_acquire(...); store.release(handle); with handle:
        mutate_filesystem()` would silently run the body with NO LOCK
        HELD AT ALL, since `__exit__` (correctly, per Finding #2) no
        -ops on an already-released handle. Entering a dead handle must
        never look like entering a live one. The check runs under the
        same `_release_lock` as `__exit__`/`release()` for consistent,
        synchronized visibility of `_released`.
        """
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
        """Round-4 review fix (Codex): calls `store.release(self)`
        exactly once per handle, and never silently does nothing when
        the handle cannot actually be released.

        - If `release()` was ALREADY called explicitly on this handle
          before the `with` block ended (both backends' `release()`
          set `_released = True` as their very first action, success
          or failure, under the SAME `_release_lock` used here), this
          is a clean no-op -- calling release() a SECOND time here
          would raise AliasLockOwnershipLostError (the connection is
          already closed) and, if the `with` body itself had raised,
          that cleanup error would MASK the body's real exception.
          Codex reproduced exactly this double-release failure mode.
        - If `_store` is unset (a handle built manually, never via
          `try_acquire()`), this raises loudly (`RuntimeError`) rather
          than silently doing nothing -- a `with handle:` that cannot
          possibly release anything must never pretend it succeeded
          (this project's anti-silent-failure standard).
        - Otherwise, `store.release(self)` runs normally. If the `with`
          body already raised AND `release()` also raises, Python's
          own `with`-statement machinery automatically chains the
          body's original exception onto the new one via
          `__context__` (verified empirically -- no manual chaining
          code needed here); the caller can still recover it via
          `exc.__context__`.

        Round-4 review fix (Finding #5): the check-then-call sequence
        is now performed under `self._release_lock` (a per-handle
        `RLock`, reentrant so this same call into `store.release(self)`
        -- which acquires the SAME lock again for its own atomic
        check-and-set -- never deadlocks). This closes the
        Codex-reproduced race where two threads could both observe
        `_released is False` and both attempt a real release.
        """
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


class AliasLockStore(Protocol):
    """Protocol implemented by both the SQLite and PostgreSQL alias lock stores."""

    def try_acquire(
        self,
        lock_key: str,
        operation: str,
        owner_token: Optional[str] = None,
    ) -> Optional[AliasLockHandle]:
        """Attempt to acquire the lock for `lock_key`.

        Args:
            lock_key: Canonical lock key (bare alias, or an opaque string
                for non-golden-repo keys such as "cidx-meta").
            operation: Human-readable operation name recorded for
                observability (e.g. "add_golden_repo").
            owner_token: Optional caller-supplied token. A random UUID4 is
                generated when omitted -- callers only need to pass this
                explicitly in tests that want a deterministic token.

        Returns:
            An `AliasLockHandle` holding the lock's live connection if
            acquired, or `None` if the lock is already held by someone
            else.
        """
        ...

    def release(self, handle: AliasLockHandle) -> None:
        """Release a held lock via an exact-token DELETE on its connection.

        Raises:
            AliasLockOwnershipLostError: if zero rows were deleted (the
                lock's connection died and someone/something else already
                observed it as free), OR if the connection itself is no
                longer usable (e.g. it was closed out from under the
                handle to simulate a crash).
        """
        ...

    def renew(self, handle: AliasLockHandle) -> None:
        """Diagnostic-only heartbeat; never used for ownership decisions.

        Raises:
            AliasLockOwnershipLostError: only when the exact-token UPDATE
                affects zero rows. A connection-level or other database
                failure propagates as its own underlying exception type,
                unwrapped, and leaves the connection untouched.
        """
        ...

    def is_held(self, lock_key: str) -> bool:
        """Issue #1546 Phase 2: non-blocking probe of whether `lock_key`
        is CURRENTLY held by ANYONE, WITHOUT acquiring the lock for keeps.

        Needed by ``WriteLockManager``'s DB-backed dispatch to implement
        the legacy file-based ``is_locked()``/``get_lock_info()`` API's
        cross-process visibility contract.

        Architecture fact this method's contract follows from (see this
        module's own docstring: the lock IS a held, UNCOMMITTED
        transaction, committed exactly once, at ``release()``): the row
        is genuinely INVISIBLE to any other connection for as long as it
        is held -- a plain SELECT on a different connection cannot see
        an uncommitted INSERT in another connection's open transaction,
        on EITHER backend. There is therefore no way to observe WHO
        holds a lock from outside it -- only WHETHER something holds it,
        via a bounded, ROLLED-BACK-BEFORE-RETURNING attempt using the
        exact same acquire mechanism ``try_acquire()`` uses (never an
        independent read path, since none exists that could see the
        data). This is why the contract is a plain boolean, never a
        metadata dict.

        Returns:
            ``True`` if `lock_key` is currently held by any connection
            (including this store's own, from a different handle).
            ``False`` if it is not currently held. Bounded by a small,
            backend-specific timeout distinct from the acquire path's
            configured contention wait -- never an indefinite block.
        """
        ...
