"""PostgreSQL-backed AliasLockStore (Issue #1546 Phase 1).

Uses a DEDICATED psycopg connection per held lock, deliberately NOT the
shared application ConnectionPool (server/storage/postgres/connection_pool
.py): that pool's context manager returns connections to the pool as soon
as the `with` block exits, but a lock's transaction must stay open across
the caller's entire acquire()...release() lifetime, which can legitimately
span hours (Bug #1218's indexing-path invariant). Borrowing a pooled
connection for that long would starve the shared pool used by unrelated
application code.

psycopg is imported lazily (see connection_pool.py's Bug #1468 precedent)
so importing this module never forces psycopg to load for callers that
never construct a PostgresAliasLockStore.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Optional

from .base import AliasLockHandle, AliasLockOwnershipLostError

if TYPE_CHECKING:
    import psycopg  # noqa: F401  (type annotations only)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS golden_repo_alias_locks (
    lock_key         TEXT PRIMARY KEY,
    owner_token      TEXT NOT NULL UNIQUE,
    operation        TEXT NOT NULL,
    acquired_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_renewed_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

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


def _is_connection_closed(conn: "psycopg.Connection") -> bool:
    return bool(getattr(conn, "closed", False))


def _require_live_connection(handle: AliasLockHandle) -> Any:
    """Raise a loud AliasLockOwnershipLostError if the handle's connection
    was already closed (real crash, or a test simulating one)."""
    conn = handle._connection
    if _is_connection_closed(conn):
        raise AliasLockOwnershipLostError(
            f"lock_key={handle.lock_key!r} owner_token={handle.owner_token!r}: "
            f"underlying connection is closed -- ownership already lost"
        )
    return conn


class PostgresAliasLockStore:
    """Session-held-transaction alias lock store backed by PostgreSQL."""

    def __init__(self, dsn: str) -> None:
        """
        Args:
            dsn: PostgreSQL connection string, e.g.
                "postgresql://user:pass@host/dbname".
        """
        self._dsn = dsn
        conn = _connect(dsn)
        try:
            conn.execute(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def try_acquire(
        self,
        lock_key: str,
        operation: str,
        owner_token: Optional[str] = None,
    ) -> Optional[AliasLockHandle]:
        owner_token = owner_token or str(uuid.uuid4())

        conn = _connect(self._dsn)
        try:
            cursor = conn.execute(_ACQUIRE_SQL, (lock_key, owner_token, operation))
            row = cursor.fetchone()
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

            # Someone else already holds lock_key.
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
        what releases the row lock PostgreSQL held on that INSERT."""
        conn = _require_live_connection(handle)
        try:
            cursor = conn.execute(_RELEASE_SQL, (handle.lock_key, handle.owner_token))
            if cursor.rowcount == 0:
                conn.rollback()
                raise AliasLockOwnershipLostError(
                    f"release() found zero rows for lock_key={handle.lock_key!r} "
                    f"owner_token={handle.owner_token!r} -- ownership already lost"
                )
            conn.commit()
        finally:
            conn.close()

    def renew(self, handle: AliasLockHandle) -> None:
        """Diagnostic-only heartbeat: exact-token UPDATE of
        last_renewed_at. Deliberately does NOT commit on success --
        committing would end the held transaction and release the row
        lock that IS this alias's lock, defeating the entire
        session-held-lock design (see release()'s docstring: the
        transaction is committed exactly once, there)."""
        conn = _require_live_connection(handle)
        try:
            cursor = conn.execute(_RENEW_SQL, (handle.lock_key, handle.owner_token))
            if cursor.rowcount == 0:
                conn.rollback()
                raise AliasLockOwnershipLostError(
                    f"renew() found zero rows for lock_key={handle.lock_key!r} "
                    f"owner_token={handle.owner_token!r} -- ownership already lost"
                )
        except Exception:
            # Any failure here means this connection's transaction is no
            # longer trustworthy -- close it rather than leaking it. A
            # SUCCESSFUL renew intentionally reaches this point WITHOUT
            # raising, and therefore WITHOUT closing the connection or
            # committing.
            conn.close()
            raise
