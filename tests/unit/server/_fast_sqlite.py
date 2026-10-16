"""SQLite without per-commit fsync for the server unit suite (#1995).

The gate scripts run the server unit suite with ``CIDX_TEST_FAST_SQLITE=1``.
``DatabaseSchema`` already honours that flag on its own bootstrap connection,
but ``synchronous`` is a per-connection setting: every other connection the
suite opens (connection manager, auth, group, audit and metrics stores) still
fsynced on every commit.  A single fixture that builds the auth stack and the
server stores issued 50-200 fsyncs; under the gate's own disk contention that
alone took many seconds and drove fixtures past the per-test timeout.

``connect_without_fsync`` is installed as ``sqlite3.connect`` for the suite by
``tests/unit/server/conftest.py`` when the flag is set.  It changes nothing
but durability against power loss, which no unit test observes.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Mapping

FAST_SQLITE_ENV = "CIDX_TEST_FAST_SQLITE"

_real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect


def fast_sqlite_enabled(environ: Mapping[str, str]) -> bool:
    """True only when the gate's flag is set to exactly ``"1"``."""
    return environ.get(FAST_SQLITE_ENV) == "1"


def connect_without_fsync(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    """``sqlite3.connect`` whose connection runs with ``synchronous=OFF``.

    ``PRAGMA synchronous`` reads the database header, so on a file that is
    locked or not a database it would fail (after waiting out the busy
    timeout) where a plain connect succeeds.  The busy timeout is dropped to
    zero for that one statement and restored afterwards; when the file cannot
    be read right now the connection is returned exactly as a plain connect
    returns it, and the caller meets the same error on its first statement.
    A failure in this setup itself closes the connection before re-raising.
    """
    conn = _real_connect(*args, **kwargs)
    try:
        busy_timeout_ms = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        conn.execute("PRAGMA busy_timeout = 0")
        try:
            conn.execute("PRAGMA synchronous = OFF")
        except sqlite3.DatabaseError:
            pass  # Unreadable now: keep the production connection unchanged.
        finally:
            conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    except BaseException:
        conn.close()
        raise
    return conn
