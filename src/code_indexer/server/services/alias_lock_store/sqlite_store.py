"""SQLite-backed AliasLockStore (Issue #1546 Phase 1).

Uses a DEDICATED ``alias_locks.db`` file, separate from the main
application database, so a multi-hour held lock transaction never blocks
unrelated application writes to unrelated tables. WAL mode is enabled so
readers (e.g. a diagnostic dump of current locks) never block on a writer.

Known SQLite characteristic (documented, not a defect): SQLite has no
row-level write locking. While one alias's lock transaction is held open,
a `BEGIN IMMEDIATE` for a DIFFERENT lock_key in the SAME `alias_locks.db`
file will also contend for the file's single writer lock and can be
delayed (bounded by `busy_timeout_seconds`) before it can even attempt its
own INSERT. This is acceptable for solo/single-node deployments (which use
SQLite specifically because they are single-node) and is the reason the
architecture calls for PostgreSQL -- true per-row locking -- in cluster
mode. `busy_timeout_seconds` is deliberately modest (not the old
file-lock's implicit unboundedness) so contention resolves to a definite
"not acquired" (`None`) rather than hanging indefinitely.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Optional, Union

from .base import AliasLockHandle, AliasLockOwnershipLostError

_MILLISECONDS_PER_SECOND = 1000
_DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0

# Substrings SQLite uses for the specific "another connection holds the
# writer lock" condition. Any OTHER OperationalError (malformed database,
# read-only filesystem, etc.) must propagate rather than be silently
# treated as ordinary lock contention.
_LOCK_CONTENTION_MESSAGE_SUBSTRINGS = (
    "database is locked",
    "database table is locked",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS golden_repo_alias_locks (
    lock_key         TEXT PRIMARY KEY,
    owner_token      TEXT NOT NULL UNIQUE,
    operation        TEXT NOT NULL,
    acquired_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_renewed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _rows_changed(conn: sqlite3.Connection) -> int:
    """Return SQLite's `changes()` -- rows affected by the most recent
    statement on this connection. Used instead of `cursor.rowcount` so the
    acquire/release/renew paths match the issue's specified primitive
    (`SELECT changes()`) literally, not merely an equivalent DB-API count.
    """
    row = conn.execute("SELECT changes()").fetchone()
    return int(row[0])


def _validate_busy_timeout_seconds(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"busy_timeout_seconds must be a number, got {value!r}")
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf check
        raise ValueError(f"busy_timeout_seconds must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"busy_timeout_seconds must be >= 0, got {value!r}")
    return float(value)


def _is_lock_contention_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        substring in message for substring in _LOCK_CONTENTION_MESSAGE_SUBSTRINGS
    )


def _open_and_begin_immediate(
    db_path: Path, busy_timeout_seconds: float
) -> Optional[sqlite3.Connection]:
    """Open a dedicated connection and start a BEGIN IMMEDIATE transaction.

    Returns the connection (transaction open, not yet committed) on
    success, or None if the writer lock specifically could not be obtained
    within busy_timeout_seconds -- a definite "someone else is using this
    file right now" signal, never an indefinite wait. Any OTHER
    OperationalError (malformed database, unreadable file, etc.), or any
    other exception at all during this setup sequence, is a genuine
    operational failure: the connection is always closed before
    propagating, and only the specific busy/locked OperationalError is
    ever translated into a "not acquired" `None`.
    """
    # isolation_level=None -> autocommit off, so we control BEGIN/COMMIT
    # explicitly (needed for BEGIN IMMEDIATE, which Python's implicit
    # "deferred" transaction start does not give us).
    conn = sqlite3.connect(
        str(db_path), timeout=busy_timeout_seconds, isolation_level=None
    )
    try:
        busy_timeout_ms = int(busy_timeout_seconds * _MILLISECONDS_PER_SECOND)
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        conn.close()
        if _is_lock_contention_error(exc):
            return None
        raise
    except Exception:
        conn.close()
        raise
    return conn


class SqliteAliasLockStore:
    """Session-held-transaction alias lock store backed by a dedicated SQLite file."""

    def __init__(
        self,
        db_path: Union[str, Path],
        *,
        busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            db_path: Path to the dedicated `alias_locks.db` file (created,
                along with parent directories, if it does not exist).
            busy_timeout_seconds: How long an acquire attempt will wait on
                file-lock contention before giving up. Bounded and finite
                by design -- never indefinite.
        """
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_seconds = _validate_busy_timeout_seconds(
            busy_timeout_seconds
        )

        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
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

        conn = _open_and_begin_immediate(self._db_path, self._busy_timeout_seconds)
        if conn is None:
            return None

        try:
            conn.execute(
                "INSERT OR IGNORE INTO golden_repo_alias_locks "
                "(lock_key, owner_token, operation) VALUES (?, ?, ?)",
                (lock_key, owner_token, operation),
            )
            if _rows_changed(conn) == 1:
                # Acquired. Leave the transaction OPEN -- this uncommitted
                # transaction, on this connection, IS the lock. Do not
                # commit or close here.
                return AliasLockHandle(
                    lock_key=lock_key,
                    owner_token=owner_token,
                    operation=operation,
                    _connection=conn,
                )

            # Someone else already holds lock_key. Roll back and close --
            # this connection has no further purpose.
            conn.execute("ROLLBACK")
            conn.close()
            return None
        except Exception:
            conn.close()
            raise

    def release(self, handle: AliasLockHandle) -> None:
        """Exact-token DELETE on the SAME connection/transaction that acquired
        the lock, then COMMIT -- this is the ONLY point in the lock's
        lifetime where the held transaction is ever committed, and doing so
        is exactly what releases the underlying SQLite writer lock (the
        mechanism that WAS this alias's lock)."""
        conn = _require_live_connection(handle)
        try:
            conn.execute(
                "DELETE FROM golden_repo_alias_locks "
                "WHERE lock_key = ? AND owner_token = ?",
                (handle.lock_key, handle.owner_token),
            )
            if _rows_changed(conn) == 0:
                conn.execute("ROLLBACK")
                raise AliasLockOwnershipLostError(
                    f"release() found zero rows for lock_key={handle.lock_key!r} "
                    f"owner_token={handle.owner_token!r} -- ownership already lost"
                )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def renew(self, handle: AliasLockHandle) -> None:
        """Diagnostic-only heartbeat: exact-token UPDATE of last_renewed_at.

        Deliberately does NOT commit on success. Committing would end the
        held transaction and release the underlying SQLite writer lock
        that IS this alias's lock -- defeating the entire session-held-lock
        design, under which the lock's connection keeps exactly ONE
        transaction open for its whole lifetime and commits it exactly
        once, in release(). The UPDATE here is visible to this same
        connection immediately (it can query its own uncommitted writes)
        and durably persisted the moment release() eventually commits;
        no other connection can observe or need to observe it earlier,
        since renew() carries no TTL/lease semantics for anyone else to
        act on.
        """
        conn = _require_live_connection(handle)
        try:
            conn.execute(
                "UPDATE golden_repo_alias_locks "
                "SET last_renewed_at = CURRENT_TIMESTAMP "
                "WHERE lock_key = ? AND owner_token = ?",
                (handle.lock_key, handle.owner_token),
            )
            if _rows_changed(conn) == 0:
                conn.execute("ROLLBACK")
                raise AliasLockOwnershipLostError(
                    f"renew() found zero rows for lock_key={handle.lock_key!r} "
                    f"owner_token={handle.owner_token!r} -- ownership already lost"
                )
        except Exception:
            # Renewal is diagnostic-only, but any failure here (ownership
            # loss or otherwise) means this connection's transaction is no
            # longer trustworthy -- close it rather than leaking it. A
            # SUCCESSFUL renew intentionally reaches this point WITHOUT
            # raising, and therefore WITHOUT closing the connection or
            # committing -- see the docstring above for why.
            conn.close()
            raise


def _require_live_connection(handle: AliasLockHandle) -> sqlite3.Connection:
    """Raise a loud AliasLockOwnershipLostError if the handle's connection
    was already closed (real crash, or a test simulating one) instead of
    letting a raw sqlite3.ProgrammingError leak through."""
    conn: sqlite3.Connection = handle._connection
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        raise AliasLockOwnershipLostError(
            f"lock_key={handle.lock_key!r} owner_token={handle.owner_token!r}: "
            f"underlying connection is closed -- ownership already lost"
        ) from exc
    return conn
