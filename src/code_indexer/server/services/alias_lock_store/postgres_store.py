"""PostgreSQL-backed AliasLockStore (Issue #1546 Phase 1).

Uses a DEDICATED psycopg connection per held lock, deliberately NOT the
shared application ConnectionPool (server/storage/postgres/connection_pool
.py): that pool's context manager returns connections to the pool as soon
as the `with` block exits, but a lock's transaction must stay open across
the caller's entire acquire()...release() lifetime, which can legitimately
span hours (Bug #1218's indexing-path invariant). Borrowing a pooled
connection for that long would starve the shared pool used by unrelated
application code.

Schema is owned EXCLUSIVELY by migration 043
(storage/postgres/migrations/sql/043_golden_repo_alias_locks.sql), applied
through the project's normal `MigrationRunner` (Story #1164's PG
advisory-lock-guarded concurrent-startup path) -- this module issues NO
DDL of its own.

psycopg is imported lazily (see connection_pool.py's Bug #1468 precedent)
so importing this module never forces psycopg to load for callers that
never construct a PostgresAliasLockStore.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from .base import AliasLockHandle, AliasLockOwnershipLostError

# Fix #1 (Issue #1546 review): PostgreSQL's `INSERT ... ON CONFLICT DO
# NOTHING` blocks on the conflicting row when that row belongs to an
# uncommitted transaction -- which, under this store's session-held
# -transaction design, is ALWAYS true for a currently-held lock. Without
# an explicit bound, a contended try_acquire() blocks for as long as the
# holder keeps the lock (proven empirically: an 8+ second block that only
# ends when the holder releases, after which the "blocked" call actually
# SUCCEEDS -- never returning None). `lock_timeout` bounds exactly that
# wait: PostgreSQL cancels the statement with `LockNotAvailable` once the
# timeout elapses, which we translate to a clean `None` (genuine
# contention), never an indefinite wait.
_DEFAULT_ACQUIRE_LOCK_TIMEOUT_SECONDS = 0.5
_MILLISECONDS_PER_SECOND = 1000
_MIN_LOCK_TIMEOUT_MS = 1

_ACQUIRE_SQL = """
INSERT INTO golden_repo_alias_locks (lock_key, owner_token, operation)
VALUES (%s, %s, %s)
ON CONFLICT (lock_key) DO NOTHING
RETURNING lock_key
"""

_RELEASE_SQL = """
DELETE FROM golden_repo_alias_locks
WHERE lock_key = %s AND owner_token = %s
"""

_RENEW_SQL = """
UPDATE golden_repo_alias_locks
SET last_renewed_at = CURRENT_TIMESTAMP
WHERE lock_key = %s AND owner_token = %s
"""


def _validate_non_empty_str(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _validate_acquire_lock_timeout_seconds(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(
            f"acquire_lock_timeout_seconds must be a number, got {value!r}"
        )
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf check
        raise ValueError(f"acquire_lock_timeout_seconds must be finite, got {value!r}")
    if value <= 0:
        raise ValueError(
            f"acquire_lock_timeout_seconds must be > 0 (a value of 0 disables "
            f"PostgreSQL's lock_timeout entirely, reintroducing indefinite "
            f"blocking), got {value!r}"
        )
    return float(value)


def _lock_timeout_milliseconds(acquire_lock_timeout_seconds: float) -> int:
    """Convert to whole milliseconds for `SET lock_timeout`, clamped to a
    floor of 1ms so a very small (but validated-positive) timeout can
    never truncate to 0 -- '0ms' means "no timeout" in PostgreSQL, which
    would silently reintroduce Fix #1's indefinite-blocking bug."""
    ms = int(acquire_lock_timeout_seconds * _MILLISECONDS_PER_SECOND)
    return max(ms, _MIN_LOCK_TIMEOUT_MS)


def _connect(dsn: str) -> Any:
    """Lazily import psycopg and open one dedicated connection.

    Autocommit is deliberately left False (psycopg's default): every
    statement after connect() participates in one open transaction until
    an explicit commit()/rollback(), which is exactly the session-held
    -lock mechanism this module implements.

    Return type is deliberately `Any` (not `psycopg.Connection[...]`):
    psycopg is imported lazily here (Bug #1468 discipline), so mypy
    cannot statically resolve the generic row-factory parameter of the
    connection returned by a dynamically-imported `psycopg.connect()`.
    """
    import psycopg

    return psycopg.connect(dsn)


def _execute_acquire_insert(
    conn: Any, lock_key: str, owner_token: str, operation: str
) -> Optional[Any]:
    """Run the acquire INSERT, translating a bounded lock-wait timeout
    into `None` (genuine contention) rather than letting it propagate --
    extracted so `try_acquire()` stays under the per-method line budget.
    Returns the fetched row (non-None means acquired), or None if the
    lock was not acquired (contention OR ON CONFLICT DO NOTHING skip)."""
    import psycopg

    try:
        cursor = conn.execute(_ACQUIRE_SQL, (lock_key, owner_token, operation))
    except psycopg.errors.LockNotAvailable:
        # Genuine contention: someone else holds lock_key's row lock and
        # we hit our own bounded wait -- a clean, fast "not acquired"
        # signal, never an indefinite block.
        return None
    return cursor.fetchone()


def _is_connection_closed(conn: Any) -> bool:
    """`conn` is typed `Any` for the same reason `_require_live_connection`
    below returns `Any`: psycopg is imported lazily in this module (Bug
    #1468 discipline), so mypy cannot statically resolve the concrete
    `psycopg.Connection[...]` type here without an eager top-level
    import."""
    return bool(getattr(conn, "closed", False))


def _is_dead_connection_error(exc: BaseException) -> bool:
    """Fix #4: classify whether `exc` indicates the underlying PostgreSQL
    connection/session is no longer usable -- e.g. a server-side
    disconnect (`psycopg.errors.AdminShutdown` from an admin
    `pg_terminate_backend`, or an idle-in-transaction timeout). This is
    DIFFERENT from `_is_connection_closed()`: empirically, `conn.closed`
    stays False locally DURING the failing call and only flips to True
    as a side effect AFTERWARD -- so a caller that only pre-checks
    `conn.closed` misses this class entirely on the first failing
    attempt. `psycopg.OperationalError` is the right classifier: every
    connection-is-dead error in psycopg (AdminShutdown, connection
    reset, etc.) is a subclass of it, while application-level errors
    (e.g. constraint violations) are not.
    """
    import psycopg

    return isinstance(exc, psycopg.OperationalError)


def _require_live_connection(handle: AliasLockHandle) -> Any:
    """Raise a loud AliasLockOwnershipLostError if the handle's connection
    was already closed (real crash, or a test simulating one).

    Return type is deliberately `Any` (not `psycopg.Connection[...]`),
    matching `_connect()`'s own rationale: psycopg is imported lazily in
    this module (Bug #1468 discipline), so mypy cannot statically
    resolve the generic row-factory parameter of a connection produced
    by a dynamically-imported `psycopg.connect()`.
    """
    conn = handle._connection
    if _is_connection_closed(conn):
        raise AliasLockOwnershipLostError(
            f"lock_key={handle.lock_key!r} owner_token={handle.owner_token!r}: "
            f"underlying connection is closed -- ownership already lost"
        )
    return conn


class PostgresAliasLockStore:
    """Session-held-transaction alias lock store backed by PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        acquire_lock_timeout_seconds: float = _DEFAULT_ACQUIRE_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            dsn: PostgreSQL connection string, e.g.
                "postgresql://user:pass@host/dbname".
            acquire_lock_timeout_seconds: Bound on how long a contended
                try_acquire() waits for the conflicting row's lock before
                giving up and returning None (Fix #1). Must be > 0 --
                never indefinite.
        """
        self._dsn = _validate_non_empty_str(dsn, "dsn")
        self._acquire_lock_timeout_seconds = _validate_acquire_lock_timeout_seconds(
            acquire_lock_timeout_seconds
        )

    def try_acquire(
        self,
        lock_key: str,
        operation: str,
        owner_token: Optional[str] = None,
    ) -> Optional[AliasLockHandle]:
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        operation = _validate_non_empty_str(operation, "operation")
        owner_token = owner_token or str(uuid.uuid4())

        conn = _connect(self._dsn)
        try:
            lock_timeout_ms = _lock_timeout_milliseconds(
                self._acquire_lock_timeout_seconds
            )
            # Bounds ONLY the wait for the conflicting row's lock inside
            # the INSERT below (Fix #1) -- deliberately a plain SET, not
            # SET LOCAL, since this connection's whole lifetime is one
            # long-held transaction and no later statement needs a
            # different value.
            conn.execute(f"SET lock_timeout = '{lock_timeout_ms}ms'")

            row = _execute_acquire_insert(conn, lock_key, owner_token, operation)
            if row is not None:
                # Acquired. Leave the transaction OPEN -- this uncommitted
                # transaction, on this connection, IS the lock. Do not
                # commit or close here.
                return AliasLockHandle(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    operation=operation,
                    _connection=conn,
                )

            # Not acquired -- either genuine contention (LockNotAvailable)
            # or the INSERT ran but ON CONFLICT DO NOTHING skipped it.
            conn.rollback()
            conn.close()
            return None
        except Exception:
            conn.close()
            raise

    def release(self, handle: AliasLockHandle) -> None:
        """Exact-token DELETE on the SAME connection/transaction that
        acquired the lock, then COMMIT -- the only point in the lock's
        lifetime this transaction is ever committed, which is exactly
        what releases the row lock PostgreSQL held on that INSERT.

        Fix #4: a genuine execution failure (e.g. a server-side
        disconnect) during EITHER the DELETE or the COMMIT is normalized
        to AliasLockOwnershipLostError, chaining the original exception
        as cause, rather than leaking a raw psycopg exception. The
        deliberate AliasLockOwnershipLostError raised below for the
        zero-rows-affected case passes through this normalization
        unchanged -- it is not itself a connection failure.
        """
        conn = _require_live_connection(handle)
        try:
            try:
                cursor = conn.execute(
                    _RELEASE_SQL, (handle.lock_key, handle.owner_token)
                )
                if cursor.rowcount == 0:
                    conn.rollback()
                    raise AliasLockOwnershipLostError(
                        f"release() found zero rows for "
                        f"lock_key={handle.lock_key!r} "
                        f"owner_token={handle.owner_token!r} -- ownership "
                        f"already lost"
                    )
                conn.commit()
            except AliasLockOwnershipLostError:
                raise
            except Exception as exc:
                if _is_dead_connection_error(exc):
                    raise AliasLockOwnershipLostError(
                        f"lock_key={handle.lock_key!r} "
                        f"owner_token={handle.owner_token!r}: release() "
                        f"failed -- connection is no longer usable"
                    ) from exc
                raise
        finally:
            conn.close()

    def renew(self, handle: AliasLockHandle) -> None:
        """Diagnostic-only heartbeat: exact-token UPDATE of
        last_renewed_at. Deliberately does NOT commit on success --
        committing would end the held transaction and release the row
        lock that IS this alias's lock, defeating the entire
        session-held-lock design (see release()'s docstring: the
        transaction is committed exactly once, there).

        Fix #3: a WRONG-TOKEN renew() (the UPDATE executes fine but
        matches zero rows) raises WITHOUT rolling back or closing the
        connection -- the real held transaction (whatever it actually
        is) stays open and the lock stays held. renew() is diagnostic
        -only and must NEVER be capable of releasing the lock on a
        caller's mistake; only release() ends the lock's lifecycle.

        Fix #4: a genuine execution failure (e.g. a server-side
        disconnect) during the UPDATE itself IS normalized to
        AliasLockOwnershipLostError and DOES close the connection --
        that failure means the connection/transaction is no longer
        trustworthy at all, which is a different situation from a
        cleanly-executed UPDATE that simply matched zero rows.
        """
        conn = _require_live_connection(handle)
        try:
            cursor = conn.execute(_RENEW_SQL, (handle.lock_key, handle.owner_token))
        except Exception as exc:
            if _is_dead_connection_error(exc):
                conn.close()
                raise AliasLockOwnershipLostError(
                    f"lock_key={handle.lock_key!r} owner_token={handle.owner_token!r}: "
                    f"renew() statement failed -- connection is no longer usable"
                ) from exc
            raise

        if cursor.rowcount == 0:
            # Wrong token: the UPDATE executed cleanly but matched zero
            # rows. Do NOT rollback, do NOT close -- the real lock (this
            # connection's actual open transaction) must remain held.
            raise AliasLockOwnershipLostError(
                f"renew() found zero rows for lock_key={handle.lock_key!r} "
                f"owner_token={handle.owner_token!r} -- ownership already lost"
            )
        # Success: leave the connection/transaction open, untouched.
