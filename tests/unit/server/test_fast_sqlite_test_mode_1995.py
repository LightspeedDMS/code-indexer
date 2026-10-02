"""Server unit tests open SQLite without per-commit fsync in test mode (#1995).

Gate runs set ``CIDX_TEST_FAST_SQLITE=1``.  Under that flag every SQLite
connection opened by the server unit suite runs with ``synchronous=OFF``:
fixtures that bootstrap the auth, group, audit and server stores were
spending most of their wall time in fsync, which under the gate's own disk
contention pushed them past the per-test timeout.

The switch must never change what a connection does other than skipping the
sync: the caller's busy timeout is kept, and a database that cannot be read
right now (corrupt, or exclusively locked) is left exactly as a plain
``sqlite3.connect`` would return it -- no new error at connect, no wait.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from tests.unit.server._fast_sqlite import (
    FAST_SQLITE_ENV,
    connect_without_fsync,
    fast_sqlite_enabled,
)

_SYNCHRONOUS_OFF = 0
_SYNCHRONOUS_FULL = 2


def _synchronous(conn: sqlite3.Connection) -> int:
    value: int = conn.execute("PRAGMA synchronous").fetchone()[0]
    return value


def _busy_timeout_ms(conn: sqlite3.Connection) -> int:
    value: int = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    return value


def test_fresh_database_connection_does_not_fsync(tmp_path: Path) -> None:
    conn = connect_without_fsync(str(tmp_path / "fresh.db"))
    try:
        assert _synchronous(conn) == _SYNCHRONOUS_OFF
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        conn.close()


def test_callers_busy_timeout_is_kept(tmp_path: Path) -> None:
    conn = connect_without_fsync(str(tmp_path / "timeout.db"), timeout=7.5)
    try:
        assert _busy_timeout_ms(conn) == 7500
        assert _synchronous(conn) == _SYNCHRONOUS_OFF
    finally:
        conn.close()


def test_corrupt_file_fails_where_a_plain_connect_fails(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not an sqlite database " * 64)

    conn = connect_without_fsync(str(path))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not a database"):
            conn.execute("SELECT name FROM sqlite_master")
    finally:
        conn.close()


def test_locked_database_returns_promptly_with_timeout_kept(tmp_path: Path) -> None:
    path = tmp_path / "locked.db"
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("CREATE TABLE t (x INTEGER)")
    holder.execute("BEGIN EXCLUSIVE")
    conn = None
    try:
        try:
            started = time.monotonic()
            conn = connect_without_fsync(str(path), timeout=5.0)
            elapsed = time.monotonic() - started
        finally:
            holder.execute("COMMIT")
            holder.close()
        assert elapsed < 1.0, f"connect waited {elapsed:.2f}s on the lock"
        assert _busy_timeout_ms(conn) == 5000
        # Left exactly as a plain connect returns it.
        assert _synchronous(conn) == _SYNCHRONOUS_FULL
    finally:
        if conn is not None:
            conn.close()


class _FailingSetupConnection(sqlite3.Connection):
    """A real connection whose busy-timeout read fails; records close()."""

    closed_instances: "list[_FailingSetupConnection]" = []

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # type: ignore[override]
        if "busy_timeout" in sql:
            raise sqlite3.OperationalError("simulated setup failure")
        return super().execute(sql, *args)

    def close(self) -> None:
        _FailingSetupConnection.closed_instances.append(self)
        super().close()


def test_connection_is_closed_when_setup_fails(tmp_path: Path) -> None:
    _FailingSetupConnection.closed_instances.clear()

    with pytest.raises(sqlite3.OperationalError, match="simulated setup failure"):
        connect_without_fsync(
            str(tmp_path / "setup.db"), factory=_FailingSetupConnection
        )

    assert len(_FailingSetupConnection.closed_instances) == 1


def test_enabled_only_by_the_exact_flag_value() -> None:
    assert fast_sqlite_enabled({FAST_SQLITE_ENV: "1"})
    assert not fast_sqlite_enabled({})
    assert not fast_sqlite_enabled({FAST_SQLITE_ENV: "0"})
    assert not fast_sqlite_enabled({FAST_SQLITE_ENV: "true"})


@pytest.mark.skipif(
    os.environ.get(FAST_SQLITE_ENV) != "1",
    reason="the switch is installed only when the gate sets the flag",
)
def test_suite_connections_skip_fsync_under_the_flag(tmp_path: Path) -> None:
    assert sqlite3.connect is connect_without_fsync
    conn = sqlite3.connect(str(tmp_path / "suite.db"))
    try:
        assert _synchronous(conn) == _SYNCHRONOUS_OFF
    finally:
        conn.close()
