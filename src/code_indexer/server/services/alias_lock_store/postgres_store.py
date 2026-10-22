"""PostgreSQL-backed AliasLockStore: a committed, row-owned lease.

See ``base.py`` for the lock model. Every operation opens ONE dedicated
connection (dead-peer detection applied), runs one short transaction and
closes it -- no session or transaction outlives a call, so a write mode
entered on one worker is released by whichever worker serves the exit, and
nothing pins ``GetOldestXmin``.

Schema: migration 043 (table) + migration 077 (holder/lease columns),
applied by ``MigrationRunner``; this module issues no DDL.

psycopg is imported lazily (Bug #1468 discipline) so importing this module
never loads psycopg for callers that never construct the store.

Rolling upgrade: a node still running the previous release holds its lock
as an UNCOMMITTED insert. That row is invisible to SELECT, but any INSERT
for the same key blocks on it; the bounded ``lock_timeout`` turns that wait
into "held" for both ``try_acquire`` and ``is_held``.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Optional, Tuple

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

_DEFAULT_ACQUIRE_LOCK_TIMEOUT_SECONDS = 0.5
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
_MILLISECONDS_PER_SECOND = 1000
_MIN_LOCK_TIMEOUT_MS = 1
_IS_HELD_PROBE_OPERATION = "is_held_probe"

_INSERT_SQL = """
INSERT INTO golden_repo_alias_locks
    (lock_key, owner_token, operation, hostname, pid, ttl_seconds)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (lock_key) DO NOTHING
RETURNING lock_key
"""

_SELECT_HOLDER_SQL = """
SELECT owner_token, operation, hostname, pid, ttl_seconds, acquired_at,
       EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - last_renewed_at))
FROM golden_repo_alias_locks
WHERE lock_key = %s
"""

_TAKEOVER_SQL = """
UPDATE golden_repo_alias_locks
SET owner_token = %s, operation = %s, hostname = %s, pid = %s,
    ttl_seconds = %s, acquired_at = CURRENT_TIMESTAMP,
    last_renewed_at = CURRENT_TIMESTAMP
WHERE lock_key = %s AND owner_token = %s
"""

_DELETE_SQL = """
DELETE FROM golden_repo_alias_locks WHERE lock_key = %s AND owner_token = %s
"""

# ^ invariant renew never shortens a lease: checkpoint renews pass the 1h default under 24h holds
_RENEW_SQL = """
UPDATE golden_repo_alias_locks
SET last_renewed_at = CURRENT_TIMESTAMP,
    ttl_seconds = GREATEST(COALESCE(ttl_seconds, %s), %s)
WHERE lock_key = %s AND owner_token = %s
"""

# (owner_token, operation, hostname, pid, ttl_seconds, acquired_at, elapsed)
_HolderRow = Tuple[str, str, Optional[str], Optional[int], Optional[int], Any, Any]


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


def _validate_positive_finite_seconds(value: float, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf check
        raise ValueError(f"{name} must be finite, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return float(value)


def _validate_acquire_lock_timeout_seconds(value: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
        raise ValueError(
            f"acquire_lock_timeout_seconds must be > 0 (a value of 0 disables "
            f"PostgreSQL's lock_timeout entirely, reintroducing indefinite "
            f"blocking), got {value!r}"
        )
    return _validate_positive_finite_seconds(value, "acquire_lock_timeout_seconds")


def _lock_timeout_milliseconds(acquire_lock_timeout_seconds: float) -> int:
    """Whole milliseconds for `SET lock_timeout`, floored at 1ms: '0ms'
    means "no timeout" in PostgreSQL."""
    ms = int(acquire_lock_timeout_seconds * _MILLISECONDS_PER_SECOND)
    return max(ms, _MIN_LOCK_TIMEOUT_MS)


def _connect(dsn: str, connect_timeout_seconds: float) -> Any:
    """Lazily import psycopg and open one dedicated connection (autocommit
    off: each call runs exactly one explicit transaction).

    Return type is `Any`: psycopg is imported lazily, so mypy cannot resolve
    the connection's generic row-factory parameter.
    """
    import psycopg

    from code_indexer.server.storage.postgres.dead_peer_detection import (
        apply_dead_peer_detection,
    )

    # Bug #2005: the server must drop a crashed client's session within ~60s.
    return psycopg.connect(
        apply_dead_peer_detection(dsn),
        connect_timeout=int(connect_timeout_seconds),
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


class PostgresAliasLockStore:
    """Row-owned leased alias lock store backed by PostgreSQL."""

    def __init__(
        self,
        dsn: str,
        *,
        acquire_lock_timeout_seconds: float = _DEFAULT_ACQUIRE_LOCK_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            dsn: PostgreSQL connection string.
            acquire_lock_timeout_seconds: bound on any row-lock wait (an
                in-flight takeover, or a legacy uncommitted holder). Must be
                > 0 -- never indefinite.
            connect_timeout_seconds: bound on connection establishment.
        """
        self._dsn = _validate_non_empty_str(dsn, "dsn")
        self._acquire_lock_timeout_seconds = _validate_acquire_lock_timeout_seconds(
            acquire_lock_timeout_seconds
        )
        self._connect_timeout_seconds = _validate_positive_finite_seconds(
            connect_timeout_seconds, "connect_timeout_seconds"
        )

    def _open(self) -> Any:
        conn = _connect(self._dsn, self._connect_timeout_seconds)
        try:
            ms = _lock_timeout_milliseconds(self._acquire_lock_timeout_seconds)
            conn.execute(f"SET lock_timeout = '{ms}ms'")
        except Exception:
            conn.close()
            raise
        return conn

    def try_acquire(
        self,
        lock_key: str,
        operation: str,
        owner_token: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Optional[AliasLockHandle]:
        import psycopg

        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        operation = _validate_non_empty_str(operation, "operation")
        ttl = _validate_ttl(ttl_seconds)
        token = owner_token or str(uuid.uuid4())
        holder = (local_holder_identity(), os.getpid())

        conn = self._open()
        try:
            try:
                inserted = conn.execute(
                    _INSERT_SQL, (lock_key, token, operation, *holder, ttl)
                ).fetchone()
                if inserted is None:
                    current = conn.execute(
                        _SELECT_HOLDER_SQL + " FOR UPDATE", (lock_key,)
                    ).fetchone()
                    if current is None or not _holder_is_stale(current):
                        conn.rollback()
                        return None
                    conn.execute(
                        _TAKEOVER_SQL,
                        (token, operation, *holder, ttl, lock_key, current[0]),
                    )
                    logger.info(
                        "Alias lock %r taken over from stale holder (owner=%r, pid=%r)",
                        lock_key,
                        current[1],
                        current[3],
                    )
            except psycopg.errors.LockNotAvailable:
                conn.rollback()
                return None
            conn.commit()
        finally:
            conn.close()
        return AliasLockHandle(
            lock_key=lock_key, owner_token=token, operation=operation, _store=self
        )

    def release_owned(
        self, lock_key: str, operation: str, *, owner_token: str
    ) -> LockOutcome:
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        conn = self._open()
        try:
            current = conn.execute(
                _SELECT_HOLDER_SQL + " FOR UPDATE", (lock_key,)
            ).fetchone()
            if current is None:
                conn.rollback()
                return LockOutcome.NOT_HELD
            if not _owner_matches(current, operation, owner_token):
                conn.rollback()
                return LockOutcome.OWNER_MISMATCH
            conn.execute(_DELETE_SQL, (lock_key, current[0]))
            conn.commit()
            return LockOutcome.RELEASED
        finally:
            conn.close()

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
        conn = self._open()
        try:
            current = conn.execute(
                _SELECT_HOLDER_SQL + " FOR UPDATE", (lock_key,)
            ).fetchone()
            if current is None:
                conn.rollback()
                return LockOutcome.NOT_HELD
            if not _owner_matches(current, operation, owner_token):
                conn.rollback()
                return LockOutcome.OWNER_MISMATCH
            conn.execute(
                _RENEW_SQL, (DEFAULT_LOCK_TTL_SECONDS, ttl, lock_key, current[0])
            )
            conn.commit()
            return LockOutcome.RENEWED
        finally:
            conn.close()

    def release(self, handle: AliasLockHandle) -> None:
        release_handle(self, handle)

    def renew(self, handle: AliasLockHandle, ttl_seconds: Optional[int] = None) -> None:
        renew_handle(self, handle, ttl_seconds)

    def is_held(self, lock_key: str) -> bool:
        import psycopg

        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        conn = self._open()
        try:
            try:
                probe = conn.execute(
                    _INSERT_SQL,
                    (
                        lock_key,
                        str(uuid.uuid4()),
                        _IS_HELD_PROBE_OPERATION,
                        None,
                        None,
                        None,
                    ),
                ).fetchone()
            except psycopg.errors.LockNotAvailable:
                return True  # a legacy uncommitted holder
            if probe is not None:
                return False
            current = conn.execute(_SELECT_HOLDER_SQL, (lock_key,)).fetchone()
            return current is not None and not _holder_is_stale(current)
        finally:
            conn.rollback()
            conn.close()

    def get_holder(self, lock_key: str) -> Optional[Dict[str, object]]:
        lock_key = _validate_non_empty_str(lock_key, "lock_key")
        conn = self._open()
        try:
            current = conn.execute(_SELECT_HOLDER_SQL, (lock_key,)).fetchone()
        finally:
            conn.rollback()
            conn.close()
        if current is None or _holder_is_stale(current):
            return None
        _token, operation, hostname, pid, ttl_seconds, acquired_at, _elapsed = current
        return {
            "owner": operation,
            "pid": pid,
            "hostname": hostname,
            "acquired_at": acquired_at.isoformat() if acquired_at else None,
            "ttl_seconds": ttl_seconds,
            "db_backed": True,
        }
