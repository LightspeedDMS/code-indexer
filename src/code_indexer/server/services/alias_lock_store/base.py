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
"""

from __future__ import annotations

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
            AliasLockOwnershipLostError: under the same conditions as
                `release()`.
        """
        ...
