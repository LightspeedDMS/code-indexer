"""Regression guard: LogsPostgresBackend.insert_log_batch must call
cur.executemany() on a CURSOR, not conn.executemany() on the Connection.

Issue #1241: The original insert_log_batch called conn.executemany() directly.
In psycopg3, executemany is a Cursor method only — the Connection object has
no executemany attribute.  Calling it raised AttributeError which the fail-open
handler swallowed as a WARNING, silently dropping every batched log row in
PG-cluster mode.

This test uses a psycopg3-faithful fake: Connection has NO executemany, only
the Cursor returned by conn.cursor() does.  With that fake in place the test
would have caught the AttributeError before the code reached staging.

No real psycopg / psycopg_pool packages are required — they are stubbed at
module level before the backend is imported.
"""

from __future__ import annotations

# Stub psycopg_pool and psycopg BEFORE importing the backend so this unit test
# runs without the real packages installed.
import sys
from contextlib import contextmanager
from typing import Any, Generator, List
from unittest.mock import MagicMock

if "psycopg_pool" not in sys.modules:
    sys.modules["psycopg_pool"] = MagicMock()
if "psycopg" not in sys.modules:
    sys.modules["psycopg"] = MagicMock()


# ---------------------------------------------------------------------------
# psycopg3-faithful fakes
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Mirrors psycopg3 Cursor: has execute() and executemany()."""

    def __init__(self) -> None:
        self.execute_calls: List[Any] = []
        self.executemany_calls: List[Any] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.execute_calls.append((sql, params))

    def executemany(self, sql: str, items: Any) -> None:
        self.executemany_calls.append((sql, list(items)))

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeConnection:
    """Mirrors psycopg3 Connection.

    Key psycopg3 invariant: Connection does NOT have executemany().
    Only Cursor (obtained via conn.cursor()) does.

    If production code calls conn.executemany() it raises AttributeError —
    exactly what happened on staging before Issue #1241 was fixed.
    Intentionally absent here so this test would catch any regression.
    """

    def __init__(self) -> None:
        self.cursors_created: List[_FakeCursor] = []
        self.execute_calls: List[Any] = []
        self.commit_called = False

    def cursor(self) -> _FakeCursor:
        cur = _FakeCursor()
        self.cursors_created.append(cur)
        return cur

    def execute(self, sql: str, params: Any = None) -> "_FakeConnection":
        # Connection.execute exists for single-row statements (used by insert_log).
        self.execute_calls.append((sql, params))
        return self

    def commit(self) -> None:
        self.commit_called = True

    # NO executemany — psycopg3 Connection does NOT expose this method.
    # Any conn.executemany() call will raise AttributeError (caught fail-open,
    # silently dropping all rows).  This omission is the whole point of the test.

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakePool:
    """Fake ConnectionPool whose connection() context manager yields a
    _FakeConnection (psycopg3-faithful: no executemany on the connection).
    """

    def __init__(self) -> None:
        self.conn = _FakeConnection()

    @contextmanager
    def connection(self) -> Generator[_FakeConnection, None, None]:
        yield self.conn


def _make_backend() -> Any:
    """Return a LogsPostgresBackend wired to a _FakePool, bypassing __init__
    (which calls _ensure_schema and needs a real DB connection).
    """
    from code_indexer.server.storage.postgres.logs_backend import LogsPostgresBackend

    pool = _FakePool()
    backend: LogsPostgresBackend = object.__new__(LogsPostgresBackend)
    backend._pool = pool  # type: ignore[attr-defined]
    return backend, pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_insert_log_batch_uses_cursor_executemany() -> None:
    """insert_log_batch must call cur.executemany() via conn.cursor(), NOT
    conn.executemany() directly.

    With a psycopg3-faithful fake (Connection has no executemany), calling
    conn.executemany() would raise AttributeError -> caught by fail-open ->
    WARNING logged -> commit() never called -> all rows silently dropped.

    This test would have caught the staging regression before #1241 was fixed.
    """
    backend, pool = _make_backend()

    items = [
        (
            "2024-01-01T00:00:00+00:00",
            "INFO",
            "src.test",
            "test message",
            None,
            None,
            None,
            None,
            None,
            None,
        )
    ]

    backend.insert_log_batch(items)

    conn = pool.conn

    # Batch insert must have committed — if it failed, commit is never called.
    assert conn.commit_called, (
        "commit() was never called. The batch insert silently failed (fail-open "
        "swallowed an error). Likely cause: conn.executemany() was called instead "
        "of cur.executemany(), raising AttributeError on psycopg3."
    )

    # At least one cursor must have been opened.
    assert conn.cursors_created, (
        "conn.cursor() was never called. Production code must open a cursor "
        "for executemany — the Connection object has no executemany in psycopg3."
    )

    # executemany must have been called on the CURSOR (not the connection).
    all_executemany_calls = [
        call for cur in conn.cursors_created for call in cur.executemany_calls
    ]
    assert all_executemany_calls, (
        "cur.executemany() was never called on any cursor. Either the batch was "
        "not inserted or conn.executemany() was attempted (AttributeError on real "
        "psycopg3, silently dropped by fail-open handler)."
    )

    # Confirm the Connection itself has NO executemany — this is the psycopg3
    # invariant that caused the original staging regression.
    assert not hasattr(conn, "executemany"), (
        "_FakeConnection must NOT have executemany — psycopg3 Connection does not. "
        "The fake is faithful: any conn.executemany() call raises AttributeError."
    )


def test_insert_log_batch_empty_is_noop() -> None:
    """insert_log_batch([]) must short-circuit: no cursor, no commit."""
    backend, pool = _make_backend()

    backend.insert_log_batch([])

    conn = pool.conn
    assert not conn.cursors_created, (
        "cursor() was opened for an empty batch — insert_log_batch should "
        "return immediately when items is empty."
    )
    assert not conn.commit_called, (
        "commit() was called for an empty batch — should be a no-op."
    )


def test_insert_log_batch_set_local_synchronous_commit_on_cursor() -> None:
    """SET LOCAL synchronous_commit = off must run on the cursor BEFORE
    executemany, within the same transaction (not on the connection).
    """
    backend, pool = _make_backend()

    items = [
        (
            "2024-01-01T00:00:00+00:00",
            "WARNING",
            "svc",
            "msg",
            None,
            None,
            None,
            None,
            None,
            None,
        )
    ]
    backend.insert_log_batch(items)

    conn = pool.conn
    assert conn.cursors_created, "no cursor created"

    # The SET LOCAL must appear in the cursor's execute calls.
    all_cursor_sqls = [
        sql.lower().strip()
        for cur in conn.cursors_created
        for (sql, _) in cur.execute_calls
    ]
    assert any("synchronous_commit" in s for s in all_cursor_sqls), (
        "SET LOCAL synchronous_commit = off was not found in cursor execute calls. "
        "It must be issued on the cursor within the same transaction as executemany."
    )
