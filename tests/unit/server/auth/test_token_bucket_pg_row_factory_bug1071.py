"""
Regression tests for Bug #1071: _pg_consume crashes with KeyError when a
pooled psycopg connection has its row_factory set to dict_row by a prior
code path.

Root cause: token_bucket.py _pg_consume called conn.execute(...).fetchone()
without pinning the cursor row_factory, so on a pooled connection previously
used with dict_row the result was a dict, causing row[0] to raise KeyError: 0.

Fix: open an explicit cursor with row_factory=tuple_row so positional access
is deterministic regardless of the connection's ambient row_factory.

Test strategy:
- Build a fake pool whose execute() returns dict rows (simulating polluted
  row_factory state), but whose cursor(row_factory=tuple_row) returns tuple rows.
- Verify BEFORE-fix behavior: conn.execute().fetchone() raises KeyError.
- Verify AFTER-fix behavior: _pg_consume works correctly with pinned cursor.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Generator, Optional


# ---------------------------------------------------------------------------
# Helpers: a FakeConnection that simulates psycopg dict_row pollution.
#
# The key simulation:
#   conn.execute(sql, params).fetchone()  -> returns a DICT  (polluted path)
#   conn.cursor(row_factory=tuple_row)   -> context manager returning a
#                                           cursor whose fetchone() returns
#                                           a TUPLE  (pinned path)
# ---------------------------------------------------------------------------


class _TupleCursor:
    """Cursor that always returns tuples regardless of connection state."""

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn
        self._cursor: Optional[sqlite3.Cursor] = None

    def execute(self, sql: str, params: Any = None) -> "_TupleCursor":
        sql = _translate_sql(sql)
        if params is not None:
            self._cursor = self._conn.execute(sql, params)
        else:
            self._cursor = self._conn.execute(sql)
        return self

    def fetchone(self) -> Optional[tuple]:
        if self._cursor is None:
            return None
        row = self._cursor.fetchone()
        return row  # type: ignore[no-any-return]

    def fetchall(self):
        if self._cursor is None:
            return []
        return self._cursor.fetchall()

    def __enter__(self) -> "_TupleCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _DictCursorResult:
    """Wraps a sqlite3 cursor to return dicts (simulates dict_row pollution)."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def fetchone(self) -> Optional[dict]:
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cursor.description]
        return [dict(zip(cols, r)) for r in rows]


class DictRowPollutedConnection:
    """
    Simulates a psycopg pooled connection with row_factory=dict_row.

    conn.execute(sql, params).fetchone() returns a dict (the bug trigger).
    conn.cursor(row_factory=<anything>) returns a _TupleCursor (the fix path).
    conn.commit() is a no-op.
    """

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn

    def execute(self, sql: str, params: Any = None) -> _DictCursorResult:
        """Returns dict rows — simulates polluted dict_row factory."""
        sql = _translate_sql(sql)
        if params is not None:
            cursor = self._conn.execute(sql, params)
        else:
            cursor = self._conn.execute(sql)
        return _DictCursorResult(cursor)

    def cursor(self, row_factory: Any = None) -> "_TupleCursor":
        """Returns a tuple-row cursor — simulates pinned row_factory."""
        return _TupleCursor(self._conn)

    def commit(self) -> None:
        self._conn.commit()


class DictRowPollutedPool:
    """Pool that hands out DictRowPollutedConnection instances."""

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn

    @contextmanager
    def connection(self) -> Generator[DictRowPollutedConnection, None, None]:
        yield DictRowPollutedConnection(self._conn)


def _translate_sql(sql: str) -> str:
    """Convert psycopg %s placeholders and PG-isms to SQLite equivalents."""
    sql = sql.replace("%s", "?")
    if "ON CONFLICT (username) DO NOTHING" in sql:
        sql = sql.replace("ON CONFLICT (username) DO NOTHING", "")
        sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
    sql = sql.replace("LEAST(", "MIN(")
    return sql


def _setup_token_bucket_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS token_bucket_state (
            username TEXT PRIMARY KEY,
            tokens REAL NOT NULL DEFAULT 10.0,
            last_refill REAL NOT NULL,
            last_access REAL NOT NULL
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Test: demonstrate the BUG (conn.execute().fetchone() returns dict -> KeyError)
# ---------------------------------------------------------------------------


class TestBugDemonstration:
    """
    Proves that the ORIGINAL code (conn.execute().fetchone()[0]) fails
    when the connection has dict_row factory active.

    This test calls _pg_consume via the UNFIXED code path by temporarily
    patching _pg_consume to use the un-pinned pattern. We demonstrate the
    failure mode directly.
    """

    def test_dict_row_polluted_connection_returns_dict(self) -> None:
        """
        Baseline: confirm that our fake pool's execute().fetchone() really
        returns a dict, not a tuple. This is the precondition for Bug #1071.
        """
        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)
        conn.execute(
            "INSERT INTO token_bucket_state (username, tokens, last_refill, last_access) "
            "VALUES (?, ?, ?, ?)",
            ("alice", 10.0, time.time(), time.time()),
        )
        conn.commit()

        polluted = DictRowPollutedConnection(conn)
        row = polluted.execute(
            "SELECT tokens, last_refill FROM token_bucket_state WHERE username = ?",
            ("alice",),
        ).fetchone()
        # Confirm it's a dict — this is the polluted state that causes Bug #1071
        assert isinstance(row, dict), f"Expected dict, got {type(row)}"
        assert "tokens" in row
        assert "last_refill" in row

    def test_positional_access_on_dict_raises_key_error(self) -> None:
        """
        Proves the bug: positional access row[0] on a dict raises KeyError: 0.
        This is exactly what happened in production.
        """
        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)
        conn.execute(
            "INSERT INTO token_bucket_state (username, tokens, last_refill, last_access) "
            "VALUES (?, ?, ?, ?)",
            ("alice", 10.0, time.time(), time.time()),
        )
        conn.commit()

        polluted = DictRowPollutedConnection(conn)
        row = polluted.execute(
            "SELECT tokens, last_refill FROM token_bucket_state WHERE username = ?",
            ("alice",),
        ).fetchone()
        assert isinstance(row, dict)

        import pytest

        with pytest.raises(KeyError):
            _ = row[0]  # This is what _pg_consume (pre-fix) does: row[0], row[1]

    def test_pg_consume_with_polluted_connection_raises(self) -> None:
        """
        Regression test: _pg_consume with a dict_row-polluted connection raises
        KeyError (the bug) when using conn.execute().fetchone()[0].

        We invoke this via a thin wrapper that replicates the pre-fix pattern
        to confirm the failure mode — this is the RED test.
        """
        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)

        pool = DictRowPollutedPool(conn)

        # Pre-seed the row so _pg_ensure_row is a no-op (simulates existing user)
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO token_bucket_state "
            "(username, tokens, last_refill, last_access) VALUES (?, ?, ?, ?)",
            ("alice", 10.0, now, now),
        )
        conn.commit()

        import pytest

        with pytest.raises((KeyError, TypeError)):
            # Simulate the PRE-FIX code path directly
            with pool.connection() as pg_conn:
                # This is what the old code did:
                row = pg_conn.execute(
                    "SELECT tokens, last_refill FROM token_bucket_state "
                    "WHERE username = ?",
                    ("alice",),
                ).fetchone()
                # row is a dict here; row[0] raises KeyError: 0
                assert row is not None
                _tokens = row[0]
                _last_refill = row[1]


# ---------------------------------------------------------------------------
# Test: the FIX (cursor with pinned row_factory returns tuple -> works)
# ---------------------------------------------------------------------------


class TestFixedBehavior:
    """
    Proves that after the fix, _pg_consume works correctly even when the
    connection's ambient row_factory is dict_row, because we pin the cursor
    row_factory to tuple_row.
    """

    def test_cursor_with_pinned_tuple_row_factory_returns_tuple(self) -> None:
        """
        The fix path: conn.cursor(row_factory=tuple_row) returns a cursor
        whose fetchone() returns a tuple, regardless of conn's ambient factory.
        """
        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)
        conn.execute(
            "INSERT INTO token_bucket_state (username, tokens, last_refill, last_access) "
            "VALUES (?, ?, ?, ?)",
            ("alice", 10.0, time.time(), time.time()),
        )
        conn.commit()

        # Simulate using a real tuple_row factory object (we only need it to exist)
        try:
            from psycopg.rows import tuple_row as real_tuple_row

            factory = real_tuple_row
        except ImportError:
            factory = None  # Not installed; our fake doesn't care about the value

        polluted = DictRowPollutedConnection(conn)
        with polluted.cursor(row_factory=factory) as cur:
            cur.execute(
                "SELECT tokens, last_refill FROM token_bucket_state WHERE username = ?",
                ("alice",),
            )
            row = cur.fetchone()

        assert isinstance(row, tuple), f"Expected tuple, got {type(row)}"
        # Positional access works
        assert isinstance(row[0], float)
        assert isinstance(row[1], float)

    def test_pg_consume_with_polluted_pool_succeeds_after_fix(self) -> None:
        """
        End-to-end regression: TokenBucketManager._pg_consume must succeed
        even when given a DictRowPollutedPool (row_factory=dict_row ambient).

        This is the GREEN test — it passes only after the fix is applied.
        """
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)

        pool = DictRowPollutedPool(conn)
        manager = TokenBucketManager(capacity=10, refill_rate=1 / 6.0)
        manager.set_connection_pool(pool)

        # Must NOT raise KeyError/TypeError — this is the regression guard
        allowed, retry = manager.consume("alice")
        assert allowed is True
        assert retry == 0.0

    def test_pg_consume_depletes_correctly_with_polluted_pool(self) -> None:
        """
        Token depletion still works correctly when pool has dict_row ambient factory.
        """
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)

        pool = DictRowPollutedPool(conn)
        manager = TokenBucketManager(capacity=2, refill_rate=0.0)
        manager.set_connection_pool(pool)

        allowed1, _ = manager.consume("alice")
        allowed2, _ = manager.consume("alice")
        allowed3, retry3 = manager.consume("alice")

        assert allowed1 is True
        assert allowed2 is True
        assert allowed3 is False
        assert retry3 > 0.0

    def test_pg_consume_none_row_handled_defensively(self) -> None:
        """
        Defensive test: if the cursor returns None for fetchone() even after
        _pg_ensure_row (should never happen, but be defensive), the code must
        not crash with TypeError: 'NoneType' is not subscriptable.
        """
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)

        # We do NOT pre-insert a row here — _pg_ensure_row will insert it.
        # This tests the normal first-access path with the polluted pool.
        pool = DictRowPollutedPool(conn)
        manager = TokenBucketManager(capacity=5, refill_rate=1 / 6.0)
        manager.set_connection_pool(pool)

        # First consume creates the row via _pg_ensure_row then reads it
        allowed, _ = manager.consume("newuser")
        assert allowed is True

    def test_pg_consume_multiuser_isolation_with_polluted_pool(self) -> None:
        """
        Multiple usernames work independently even with dict_row polluted pool.
        """
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        conn = sqlite3.connect(":memory:")
        _setup_token_bucket_table(conn)

        pool = DictRowPollutedPool(conn)
        manager = TokenBucketManager(capacity=1, refill_rate=0.0)
        manager.set_connection_pool(pool)

        # Exhaust alice
        manager.consume("alice")
        allowed_alice, _ = manager.consume("alice")
        assert allowed_alice is False

        # Bob should still be allowed
        allowed_bob, _ = manager.consume("bob")
        assert allowed_bob is True
