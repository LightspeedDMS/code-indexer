"""SQLite-backed AliasLockStore (solo): a committed, row-owned lease.

See ``base.py`` for the lock model. One dedicated SQLite file PER ALIAS,
``{quote(lock_key, safe="")}.db`` under a node-local lock directory (never
the ``nolock`` NFS golden-repos mount, see ``factory.py``). Per-alias files
keep unrelated aliases from ever contending on one database writer lock.
Files are never deleted; filenames are a reversible encoding of the alias so
an operator can read the directory.

Every call opens its own connection, runs ONE short ``BEGIN IMMEDIATE``
transaction (bounded by ``busy_timeout_seconds``) and closes, so any uvicorn
worker process can release a lock another worker took. Lock files from the
previous release (no holder/lease columns) are upgraded in place.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import quote

from code_indexer.global_repos.write_lock_manager import (
    DEFAULT_LOCK_TTL_SECONDS,
    lock_holder_is_stale,
)

from .base import (
    AliasLockHandle,
    LockOutcome,
    local_holder_identity,
    release_handle,
    renew_handle,
)

logger = logging.getLogger(__name__)

_MILLISECONDS_PER_SECOND = 1000
_DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0

# Only these mean "another connection holds the writer lock"; any other
# OperationalError (malformed database, read-only filesystem) propagates.
_LOCK_CONTENTION_MESSAGE_SUBSTRINGS = (
    "database is locked",
    "database table is locked",
)

_NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS golden_repo_alias_locks (
    lock_key         TEXT PRIMARY KEY,
    owner_token      TEXT NOT NULL,
    operation        TEXT NOT NULL,
    acquired_at      TEXT NOT NULL DEFAULT ({_NOW}),
    last_renewed_at  TEXT NOT NULL DEFAULT ({_NOW}),
    hostname         TEXT,
    pid              INTEGER,
    ttl_seconds      INTEGER
)
"""
_LEASE_COLUMNS = (("hostname", "TEXT"), ("pid", "INTEGER"), ("ttl_seconds", "INTEGER"))

_SELECT_HOLDER_SQL = """
SELECT owner_token, operation, hostname, pid, ttl_seconds, acquired_at,
       (julianday('now') - julianday(last_renewed_at)) * 86400.0
FROM golden_repo_alias_locks WHERE lock_key = ?
"""

# (owner_token, operation, hostname, pid, ttl_seconds, acquired_at, elapsed)
_HolderRow = Tuple[str, str, Optional[str], Optional[int], Optional[int], Any, Any]


def _validate_busy_timeout_seconds(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"busy_timeout_seconds must be a number, got {value!r}")
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf check
        raise ValueError(f"busy_timeout_seconds must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"busy_timeout_seconds must be >= 0, got {value!r}")
    return float(value)


def _validate_non_empty_str(value: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _validate_ttl(ttl_seconds: Optional[int]) -> int:
    if ttl_seconds is None:
        return DEFAULT_LOCK_TTL_SECONDS
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
        raise ValueError(f"ttl_seconds must be an int, got {ttl_seconds!r}")
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds!r}")
    return ttl_seconds


def _is_lock_contention_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        substring in message for substring in _LOCK_CONTENTION_MESSAGE_SUBSTRINGS
    )


def _db_path_for_lock_key(lock_dir: Path, lock_key: str) -> Path:
    return lock_dir / f"{quote(lock_key, safe='')}.db"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table, or add the lease columns to a previous-release file.
    Runs inside the caller's BEGIN IMMEDIATE, so concurrent upgrades serialize."""
    conn.execute(_SCHEMA_SQL)
    present = {
        row[1] for row in conn.execute("PRAGMA table_info(golden_repo_alias_locks)")
    }
    for name, sql_type in _LEASE_COLUMNS:
        if name not in present:
            conn.execute(
                f"ALTER TABLE golden_repo_alias_locks ADD COLUMN {name} {sql_type}"
            )


def _holder_is_stale(row: _HolderRow) -> bool:
    _token, _operation, hostname, pid, ttl_seconds, _acquired, elapsed = row
    return lock_holder_is_stale(
        hostname,
        pid,
        float(elapsed) if elapsed is not None else None,
        ttl_seconds,
        local_host=local_holder_identity(),
    )


def _owner_matches(row: _HolderRow, operation: str, owner_token: str) -> bool:
    return row[1] == operation and row[0] == owner_token


class SqliteAliasLockStore:
    """Row-owned leased alias lock store, one SQLite file per alias."""

    def __init__(
        self,
        lock_dir: Any,
        *,
        busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            lock_dir: node-local directory holding one SQLite file per alias
                (created if missing).
            busy_timeout_seconds: bound on waiting for another call's short
                transaction on the SAME alias's file.
        """
        self._lock_dir = Path(lock_dir)
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_seconds = _validate_busy_timeout_seconds(
            busy_timeout_seconds
        )

    @contextmanager
    def _transaction(self, lock_key: str) -> Iterator[sqlite3.Connection]:
        """One BEGIN IMMEDIATE transaction on the alias's own file: committed
        when the body returns, rolled back when it raises."""
        conn = sqlite3.connect(
            str(_db_path_for_lock_key(self._lock_dir, lock_key)),
            timeout=self._busy_timeout_seconds,
            isolation_level=None,
        )
        try:
            ms = int(self._busy_timeout_seconds * _MILLISECONDS_PER_SECOND)
            conn.execute(f"PRAGMA busy_timeout={ms}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                _ensure_schema(conn)
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def try_acquire(
        self,
        lock_key: str,
        operation: str,
        owner_token: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[AliasLockHandle]:
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        operation = _validate_non_empty_str(operation, "operation")
        ttl = _validate_ttl(ttl_seconds)
        token = owner_token or str(uuid.uuid4())
        holder = (local_holder_identity(), os.getpid())
        try:
            with self._transaction(lock_key) as conn:
                current = conn.execute(_SELECT_HOLDER_SQL, (lock_key,)).fetchone()
                if current is not None and not _holder_is_stale(current):
                    return None
                if current is not None:
                    logger.info(
                        "Alias lock %r taken over from stale holder (owner=%r, pid=%r)",
                        lock_key,
                        current[1],
                        current[3],
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO golden_repo_alias_locks "
                    "(lock_key, owner_token, operation, hostname, pid, ttl_seconds, "
                    f"acquired_at, last_renewed_at) VALUES (?, ?, ?, ?, ?, ?, {_NOW}, {_NOW})",
                    (lock_key, token, operation, *holder, ttl),
                )
        except sqlite3.OperationalError as exc:
            if _is_lock_contention_error(exc):
                return None
            raise
        return AliasLockHandle(
            lock_key=lock_key, owner_token=token, operation=operation, _store=self
        )

    def release_owned(
        self, lock_key: str, operation: str, *, owner_token: str
    ) -> LockOutcome:
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        with self._transaction(lock_key) as conn:
            current = conn.execute(_SELECT_HOLDER_SQL, (lock_key,)).fetchone()
            if current is None:
                return LockOutcome.NOT_HELD
            if not _owner_matches(current, operation, owner_token):
                return LockOutcome.OWNER_MISMATCH
            conn.execute(
                "DELETE FROM golden_repo_alias_locks WHERE lock_key = ? AND owner_token = ?",
                (lock_key, current[0]),
            )
        return LockOutcome.RELEASED

    def renew_owned(
        self,
        lock_key: str,
        operation: str,
        *,
        owner_token: str,
        ttl_seconds: Optional[int] = None,
    ) -> LockOutcome:
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        ttl = _validate_ttl(ttl_seconds)
        with self._transaction(lock_key) as conn:
            current = conn.execute(_SELECT_HOLDER_SQL, (lock_key,)).fetchone()
            if current is None:
                return LockOutcome.NOT_HELD
            if not _owner_matches(current, operation, owner_token):
                return LockOutcome.OWNER_MISMATCH
            # ^ invariant renew never shortens a lease: checkpoint renews pass the 1h default under 24h holds
            conn.execute(
                f"UPDATE golden_repo_alias_locks SET last_renewed_at = {_NOW}, "
                "ttl_seconds = MAX(COALESCE(ttl_seconds, ?), ?) "
                "WHERE lock_key = ? AND owner_token = ?",
                (DEFAULT_LOCK_TTL_SECONDS, ttl, lock_key, current[0]),
            )
        return LockOutcome.RENEWED

    def release(self, handle: AliasLockHandle) -> None:
        release_handle(self, handle)

    def renew(self, handle: AliasLockHandle, ttl_seconds: Optional[int] = None) -> None:
        renew_handle(self, handle, ttl_seconds)

    def _live_holder(self, lock_key: str) -> Optional[_HolderRow]:
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        try:
            with self._transaction(lock_key) as conn:
                current = conn.execute(_SELECT_HOLDER_SQL, (lock_key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if not _is_lock_contention_error(exc):
                raise
            # ^ why a previous-release process holds BEGIN IMMEDIATE for its whole hold
            return ("", "unknown", None, None, None, None, None)
        if current is None or _holder_is_stale(current):
            return None
        return tuple(current)  # type: ignore[return-value]

    def is_held(self, lock_key: str) -> bool:
        return self._live_holder(lock_key) is not None

    def get_holder(self, lock_key: str) -> Optional[Dict[str, object]]:
        current = self._live_holder(lock_key)
        if current is None:
            return None
        _token, operation, hostname, pid, ttl_seconds, acquired_at, _elapsed = current
        return {
            "owner": operation,
            "pid": pid,
            "hostname": hostname,
            "acquired_at": (
                f"{str(acquired_at).replace(' ', 'T')}+00:00" if acquired_at else None
            ),
            "ttl_seconds": ttl_seconds,
            "db_backed": True,
        }
