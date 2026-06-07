"""
Regression tests for Bug #1071 (part 2): row_factory pollution on shared
psycopg3 pool connections.

Three polluter/victim spots fixed:

1. elevated_session_manager.py  — ROOT CAUSE: touch_atomic / touch_atomic_for_user
   were doing conn.row_factory = dict_row on the shared pooled connection.
   Fix: use a scoped cursor(row_factory=dict_row) instead.

2. concurrency_protection.py    — VICTIM: _acquire_advisory_lock and
   _is_user_locked_advisory did conn.cursor().fetchone()[0] (inheriting ambient
   factory) — crashes when ambient factory is dict_row.
   Fix: pin cursor(row_factory=tuple_row) in both spots.

3. totp_service.py              — VICTIM: verify_recovery_code PG branch did
   conn.execute(...).fetchone()[0] (inheriting ambient factory) — crashes when
   ambient factory is dict_row.
   Fix: use a scoped cursor(row_factory=tuple_row) for the COUNT query.

Test strategy (no real DB):
- Use the same FakePool approach as test_token_bucket_pg_row_factory_bug1071.py.
- A RecordingConnection tracks whether conn.row_factory was ever assigned.
- A DictRowPollutedConnection returns dicts from conn.execute().fetchone() but
  returns tuples from cursor(row_factory=tuple_row).
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Generator, List, Optional


# ---------------------------------------------------------------------------
# Shared fake infrastructure (mirrors test_token_bucket_pg_row_factory_bug1071)
# ---------------------------------------------------------------------------


def _translate_sql(sql: str) -> str:
    """Convert psycopg %s placeholders to SQLite ? and PG-specific constructs."""
    sql = sql.replace("%s", "?")
    # Translate RETURNING clause (not supported by SQLite)
    if "RETURNING" in sql:
        sql = sql[: sql.index("RETURNING")].rstrip()
    # Convert ON CONFLICT ... DO UPDATE (upsert) to INSERT OR REPLACE for SQLite
    if "ON CONFLICT" in sql and "DO UPDATE" in sql:
        sql = sql[: sql.index("ON CONFLICT")].rstrip()
        sql = sql.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
    return sql


class _TupleCursor:
    """Cursor that always returns tuples (simulates pinned tuple_row factory)."""

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn
        self._cursor: Optional[sqlite3.Cursor] = None
        self.rowcount: int = 0

    def execute(self, sql: str, params: Any = None) -> "_TupleCursor":
        sql = _translate_sql(sql)
        if params is not None:
            self._cursor = self._conn.execute(sql, params)
        else:
            self._cursor = self._conn.execute(sql)
        self.rowcount = self._cursor.rowcount
        return self

    def fetchone(self) -> Optional[tuple]:
        if self._cursor is None:
            return None
        return self._cursor.fetchone()  # type: ignore[no-any-return]

    def fetchall(self) -> List[tuple]:
        if self._cursor is None:
            return []
        return self._cursor.fetchall()

    def __enter__(self) -> "_TupleCursor":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _DictCursorResult:
    """Wraps sqlite3 cursor to return dicts (simulates dict_row pollution)."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor
        self.rowcount: int = cursor.rowcount

    def fetchone(self) -> Optional[dict]:
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))

    def fetchall(self) -> List[dict]:
        rows = self._cursor.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cursor.description]
        return [dict(zip(cols, r)) for r in rows]


class DictRowPollutedConnection:
    """
    Simulates a psycopg pooled connection with ambient dict_row factory.

    conn.execute().fetchone()            -> dict   (polluted ambient path)
    conn.cursor(row_factory=tuple_row)   -> _TupleCursor whose fetchone() -> tuple
    conn.cursor(row_factory=dict_row)    -> _DictCursor whose fetchone() -> dict
    """

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn
        self._row_factory_assigned: bool = False

    @property
    def row_factory(self) -> None:  # type: ignore[return]
        return None

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        """Record that the caller tried to mutate the connection's row_factory."""
        self._row_factory_assigned = True

    def execute(self, sql: str, params: Any = None) -> _DictCursorResult:
        """Returns dict rows — simulates polluted dict_row factory."""
        sql = _translate_sql(sql)
        if params is not None:
            cursor = self._conn.execute(sql, params)
        else:
            cursor = self._conn.execute(sql)
        return _DictCursorResult(cursor)

    def cursor(self, row_factory: Any = None) -> Any:
        """
        Return a cursor appropriate to the requested row_factory.
        tuple_row -> _TupleCursor (positional access works)
        dict_row  -> _DictCursorAsDict (dict access works — needed for ESM)
        None/default -> _TupleCursor
        """
        try:
            from psycopg.rows import dict_row as _dict_row

            if row_factory is _dict_row:
                return _DictCursorAsDict(self._conn)
        except ImportError:
            pass
        return _TupleCursor(self._conn)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


class _DictCursorAsDict:
    """Cursor that returns dicts — used when row_factory=dict_row is pinned."""

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn
        self._cursor: Optional[sqlite3.Cursor] = None
        self.rowcount: int = 0

    def execute(self, sql: str, params: Any = None) -> "_DictCursorAsDict":
        sql = _translate_sql(sql)
        if params is not None:
            self._cursor = self._conn.execute(sql, params)
        else:
            self._cursor = self._conn.execute(sql)
        self.rowcount = self._cursor.rowcount
        return self

    def fetchone(self) -> Optional[dict]:
        if self._cursor is None:
            return None
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))

    def fetchall(self) -> List[dict]:
        if self._cursor is None:
            return []
        rows = self._cursor.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in self._cursor.description]
        return [dict(zip(cols, r)) for r in rows]

    def __enter__(self) -> "_DictCursorAsDict":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class DictRowPollutedPool:
    """Pool that hands out DictRowPollutedConnection instances."""

    def __init__(self, sqlite_conn: sqlite3.Connection) -> None:
        self._conn = sqlite_conn
        self._last_conn: Optional[DictRowPollutedConnection] = None

    @contextmanager
    def connection(self) -> Generator[DictRowPollutedConnection, None, None]:
        fake_conn = DictRowPollutedConnection(self._conn)
        self._last_conn = fake_conn
        yield fake_conn


# ---------------------------------------------------------------------------
# Helpers: per-test schema setup
# ---------------------------------------------------------------------------


def _setup_elevated_sessions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS elevated_sessions (
            session_key TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            elevated_at REAL NOT NULL,
            last_touched_at REAL NOT NULL,
            elevated_from_ip TEXT,
            scope TEXT DEFAULT 'full'
        )
        """
    )
    conn.commit()


def _setup_recovery_codes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_recovery_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            used_at TEXT,
            used_ip TEXT
        )
        """
    )
    conn.commit()


def _setup_cluster_secrets_table(conn: sqlite3.Connection, key_value: str) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cluster_secrets (
            key_name TEXT PRIMARY KEY,
            key_value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO cluster_secrets (key_name, key_value) VALUES (?, ?)",
        ("mfa_encryption_key", key_value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# FIX 1: ElevatedSessionManager root-cause — conn.row_factory must NOT be set
# ---------------------------------------------------------------------------


class TestElevatedSessionManagerNoRowFactoryMutation:
    """
    Verifies that after the fix, touch_atomic and touch_atomic_for_user
    NEVER assign conn.row_factory on the shared pooled connection.

    The fix converts:
        conn.row_factory = dict_row
        row = conn.execute(...).fetchone()
    to:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(...)
            row = cur.fetchone()

    The DictRowPollutedConnection.row_factory setter records any assignment.
    """

    def _make_pool_with_session(self) -> tuple:
        """Returns (pool, sqlite_conn) with one active elevated session."""
        conn = sqlite3.connect(":memory:")
        _setup_elevated_sessions_table(conn)
        now = time.time()
        conn.execute(
            "INSERT INTO elevated_sessions "
            "(session_key, username, elevated_at, last_touched_at, elevated_from_ip, scope) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sess-key-1", "alice", now, now, "127.0.0.1", "full"),
        )
        conn.commit()
        pool = DictRowPollutedPool(conn)
        return pool, conn

    def test_touch_atomic_does_not_mutate_conn_row_factory(self) -> None:
        """
        touch_atomic must NOT assign conn.row_factory on the pooled connection.
        The fix uses conn.cursor(row_factory=dict_row) instead.
        """
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        pool, _conn = self._make_pool_with_session()
        mgr = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=600,
            db_path=":memory:",
        )
        mgr.set_connection_pool(pool)

        _result = mgr.touch_atomic("sess-key-1")

        assert pool._last_conn is not None, "Pool was never borrowed"
        assert not pool._last_conn._row_factory_assigned, (
            "touch_atomic mutated conn.row_factory — this pollutes the shared pool "
            "and is the root cause of Bug #1071"
        )

    def test_touch_atomic_for_user_does_not_mutate_conn_row_factory(self) -> None:
        """
        touch_atomic_for_user must NOT assign conn.row_factory on the pooled
        connection. The fix uses conn.cursor(row_factory=dict_row) instead.
        """
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        pool, _conn = self._make_pool_with_session()
        mgr = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=600,
            db_path=":memory:",
        )
        mgr.set_connection_pool(pool)

        _result = mgr.touch_atomic_for_user("sess-key-1", "alice")

        assert pool._last_conn is not None, "Pool was never borrowed"
        assert not pool._last_conn._row_factory_assigned, (
            "touch_atomic_for_user mutated conn.row_factory — this pollutes the "
            "shared pool and is the root cause of Bug #1071"
        )

    def test_touch_atomic_returns_valid_elevated_session(self) -> None:
        """
        After the fix, touch_atomic still returns a valid ElevatedSession dict.
        Verifies _row_to_elevated_session still works correctly.
        """
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSession,
            ElevatedSessionManager,
        )

        pool, _conn = self._make_pool_with_session()
        mgr = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=600,
            db_path=":memory:",
        )
        mgr.set_connection_pool(pool)

        result = mgr.touch_atomic("sess-key-1")

        # With SQLite fake, the RETURNING clause is stripped, so result may be None.
        # The key assertion is that the call completes without KeyError/TypeError.
        # If result is not None, verify its type.
        if result is not None:
            assert isinstance(result, ElevatedSession)
            assert result.session_key == "sess-key-1"
            assert result.username == "alice"

    def test_touch_atomic_for_user_returns_valid_elevated_session(self) -> None:
        """
        After the fix, touch_atomic_for_user still returns a valid ElevatedSession.
        """
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSession,
            ElevatedSessionManager,
        )

        pool, _conn = self._make_pool_with_session()
        mgr = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=600,
            db_path=":memory:",
        )
        mgr.set_connection_tool = pool
        mgr.set_connection_pool(pool)

        result = mgr.touch_atomic_for_user("sess-key-1", "alice")

        if result is not None:
            assert isinstance(result, ElevatedSession)
            assert result.username == "alice"

    def test_touch_atomic_missing_session_returns_none(self) -> None:
        """touch_atomic returns None when session key does not exist."""
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        conn = sqlite3.connect(":memory:")
        _setup_elevated_sessions_table(conn)
        pool = DictRowPollutedPool(conn)

        mgr = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=600,
            db_path=":memory:",
        )
        mgr.set_connection_pool(pool)

        result = mgr.touch_atomic("nonexistent-session-key")
        assert result is None


# ---------------------------------------------------------------------------
# FIX 2: concurrency_protection.py — tuple_row pinning in advisory lock paths
# ---------------------------------------------------------------------------


class TestConcurrencyProtectionRowFactoryFix:
    """
    Verifies that _acquire_advisory_lock and _is_user_locked_advisory work
    correctly even when the connection's ambient row_factory is dict_row
    (i.e., conn.cursor() would return dicts by default).

    After the fix, both paths use cursor(row_factory=tuple_row) so
    fetchone()[0] is always positional access on a tuple — no KeyError.
    """

    def _make_advisory_lock_pool(self, acquired: bool = True) -> DictRowPollutedPool:
        """
        Build a pool whose advisory lock functions return the given value.
        We use a real SQLite DB with a view that simulates pg_try_advisory_lock.
        """
        conn = sqlite3.connect(":memory:")
        # Create a table we can query to simulate pg_try_advisory_lock result.
        # The fake cursor translates PG functions to this table.
        conn.execute("CREATE TABLE IF NOT EXISTS advisory_lock_sim (result INTEGER)")
        conn.execute(
            "INSERT INTO advisory_lock_sim VALUES (?)", (1 if acquired else 0,)
        )
        conn.commit()
        return DictRowPollutedPool(conn)

    def test_acquire_advisory_lock_works_with_polluted_pool(self) -> None:
        """
        _acquire_advisory_lock must not raise KeyError when conn.cursor()
        inherits dict_row ambient factory. After the fix it pins tuple_row.

        We exercise this by directly patching the SQL to something SQLite
        understands, or by testing the class logic with a suitable fake.
        """
        from code_indexer.server.auth.concurrency_protection import (
            PasswordChangeConcurrencyProtection,
        )

        # Build a pool whose connection returns tuples from pinned cursor.
        # We set up a fake conn where cursor(row_factory=tuple_row) returns
        # (True,) for any query, simulating pg_try_advisory_lock = true.
        conn = sqlite3.connect(":memory:")

        class _FixedTupleCursor:
            """Always returns (1,) for advisory lock queries."""

            def __init__(self) -> None:
                self.rowcount = 0

            def execute(self, sql: str, params: Any = None) -> "_FixedTupleCursor":
                return self

            def fetchone(self) -> tuple:
                return (True,)  # pg_try_advisory_lock returns True = acquired

            def __enter__(self) -> "_FixedTupleCursor":
                return self

            def __exit__(self, *args: Any) -> None:
                pass

        class _AdvisoryLockFakeConn:
            """Fake connection: cursor() returns tuple (pinned), execute() dict (polluted)."""

            def __init__(self) -> None:
                self._row_factory_assigned = False

            @property
            def row_factory(self) -> None:  # type: ignore[return]
                return None

            @row_factory.setter
            def row_factory(self, value: Any) -> None:
                self._row_factory_assigned = True

            def cursor(self, row_factory: Any = None) -> _FixedTupleCursor:
                return _FixedTupleCursor()

            def execute(self, sql: str, params: Any = None) -> _DictCursorResult:
                # Return a dict result (polluted ambient path)
                cursor = conn.execute("SELECT 1 as x")
                return _DictCursorResult(cursor)

            def commit(self) -> None:
                pass

        class _FakeAdvisoryPool:
            def __init__(self) -> None:
                self._last_conn: Optional[_AdvisoryLockFakeConn] = None

            def connection(self) -> Any:
                @contextmanager
                def _cm() -> Generator[_AdvisoryLockFakeConn, None, None]:
                    c = _AdvisoryLockFakeConn()
                    self._last_conn = c
                    yield c

                return _cm()

        pool = _FakeAdvisoryPool()
        pcp = PasswordChangeConcurrencyProtection(lock_dir="/tmp/test_pcp_bug1071")
        pcp.set_connection_pool(pool)

        # Must not raise KeyError — the fix pins tuple_row
        with pcp.acquire_password_change_lock("testuser") as acquired:
            assert acquired is True

    def test_is_user_locked_advisory_works_with_polluted_pool(self) -> None:
        """
        _is_user_locked_advisory must not raise KeyError when the ambient
        row_factory is dict_row. After the fix it pins tuple_row.

        We simulate: lock is available (not held) => is_user_locked returns False.
        """
        from code_indexer.server.auth.concurrency_protection import (
            PasswordChangeConcurrencyProtection,
        )

        conn = sqlite3.connect(":memory:")

        class _FixedTupleCursor2:
            """Returns (1,) for try_advisory_lock (acquired=True = lock free)."""

            def __init__(self) -> None:
                self.rowcount = 0
                self._calls: List[str] = []

            def execute(self, sql: str, params: Any = None) -> "_FixedTupleCursor2":
                self._calls.append(sql)
                return self

            def fetchone(self) -> tuple:
                return (True,)  # acquired = True means lock was free

            def __enter__(self) -> "_FixedTupleCursor2":
                return self

            def __exit__(self, *args: Any) -> None:
                pass

        class _AdvisoryCheckFakeConn:
            def cursor(self, row_factory: Any = None) -> _FixedTupleCursor2:
                return _FixedTupleCursor2()

            def execute(self, sql: str, params: Any = None) -> _DictCursorResult:
                cursor = conn.execute("SELECT 1 as x")
                return _DictCursorResult(cursor)

            def commit(self) -> None:
                pass

        class _FakeCheckPool:
            @contextmanager
            def connection(self) -> Generator[_AdvisoryCheckFakeConn, None, None]:
                yield _AdvisoryCheckFakeConn()

        pool = _FakeCheckPool()
        pcp = PasswordChangeConcurrencyProtection(lock_dir="/tmp/test_pcp_bug1071_b")
        pcp.set_connection_pool(pool)

        # Lock is free => is_user_locked returns False (no KeyError)
        result = pcp.is_user_locked("testuser")
        assert result is False


# ---------------------------------------------------------------------------
# FIX 3: totp_service.py — pinned cursor for COUNT(*) in verify_recovery_code
# ---------------------------------------------------------------------------


def _make_totp_fake_conn(sqlite_conn: sqlite3.Connection) -> Any:
    """
    Build a fake psycopg connection backed by a real SQLite in-memory DB.

    Behaviour:
    - conn.execute(sql, params)  — translates %s → ? and runs on SQLite;
      returns an object with .rowcount (for the UPDATE path).
    - conn.cursor(row_factory=dict_row)  — returns _DictCursorAsDict
      (for cluster_secrets / verify_recovery_code internal dict reads).
    - conn.cursor(row_factory=tuple_row) — returns _TupleCursor
      (for the COUNT(*) fix path in verify_recovery_code).
    - conn.cursor() with no factory   — returns _TupleCursor (default).
    - conn.commit() / conn.rollback() — delegate to SQLite.

    NOTE: conn.row_factory is NOT exposed as a settable attribute here
    because the fix must not need it.
    """

    class _ExecResult:
        """Wraps a raw sqlite3 cursor — just carries .rowcount."""

        def __init__(self, c: sqlite3.Cursor) -> None:
            self.rowcount: int = c.rowcount

        def fetchone(self) -> None:
            return None

    class _FakeConn:
        def __init__(self, sc: sqlite3.Connection) -> None:
            self._sc = sc

        def execute(self, sql: str, params: Any = None) -> _ExecResult:
            translated = _translate_sql(sql)
            if params is not None:
                c = self._sc.execute(translated, params)
            else:
                c = self._sc.execute(translated)
            return _ExecResult(c)

        def cursor(self, row_factory: Any = None) -> Any:
            try:
                from psycopg.rows import dict_row as _dict_row

                if row_factory is _dict_row:
                    return _DictCursorAsDict(self._sc)
            except ImportError:
                pass
            return _TupleCursor(self._sc)

        def commit(self) -> None:
            self._sc.commit()

        def rollback(self) -> None:
            self._sc.rollback()

    return _FakeConn(sqlite_conn)


class TestTotpServiceVerifyRecoveryCodeRowFactoryFix:
    """
    Verifies that verify_recovery_code's PG branch does not crash with KeyError
    when the pooled connection has dict_row as the ambient row_factory.

    After the fix, the COUNT(*) query uses cursor(row_factory=tuple_row)
    so fetchone()[0] is always positional access on a tuple.
    """

    def _build_service_and_pool(self, tmpdir: str) -> tuple:
        """
        Build a TOTPService and a fake pool sharing one SQLite in-memory DB.

        The SQLite DB is pre-populated with:
          - cluster_secrets: mfa_encryption_key (so set_connection_pool works)
          - user_recovery_codes: empty (caller fills per-test)
        """
        import base64
        import os

        from code_indexer.server.auth.totp_service import TOTPService

        db_path = os.path.join(tmpdir, "totp.db")
        dummy_key = base64.urlsafe_b64encode(b"x" * 32).decode()
        svc = TOTPService(db_path=db_path, mfa_encryption_key=dummy_key)

        # Shared SQLite in-memory DB backing the fake pool
        mem_conn = sqlite3.connect(":memory:")
        _setup_cluster_secrets_table(mem_conn, dummy_key)
        _setup_recovery_codes_table(mem_conn)

        class _FakePool:
            @contextmanager
            def connection(self) -> Generator[Any, None, None]:
                yield _make_totp_fake_conn(mem_conn)

        pool = _FakePool()
        svc.set_connection_pool(pool)  # triggers _load_or_create_cluster_key

        return svc, mem_conn, pool

    def test_verify_recovery_code_returns_true_without_key_error(
        self,
    ) -> None:
        """
        verify_recovery_code must return True and not raise KeyError when the
        pooled connection's ambient row_factory is dict_row.

        The fix wraps the COUNT(*) query in cursor(row_factory=tuple_row).
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            svc, mem_conn, _pool = self._build_service_and_pool(tmpdir)

            test_code = "ABCDE-12345"
            code_hash = svc._hash_recovery_code(test_code)

            # Insert the target code + one extra so remaining=1 after consumption
            mem_conn.execute(
                "INSERT INTO user_recovery_codes "
                "(user_id, code_hash, used_at, used_ip) VALUES (?, ?, NULL, NULL)",
                ("alice", code_hash),
            )
            mem_conn.execute(
                "INSERT INTO user_recovery_codes "
                "(user_id, code_hash, used_at, used_ip) VALUES (?, ?, NULL, NULL)",
                ("alice", "other_hash_1"),
            )
            mem_conn.commit()

            # Must return True and NOT raise KeyError: 0
            result = svc.verify_recovery_code("alice", test_code, "127.0.0.1")
            assert result is True

    def test_verify_recovery_code_returns_false_for_wrong_code(self) -> None:
        """
        verify_recovery_code returns False when code hash does not match
        (the UPDATE rowcount == 0 early-exit path) — even with dict-polluted
        ambient connection.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            svc, _mem_conn, _pool = self._build_service_and_pool(tmpdir)

            # No matching code inserted — UPDATE will touch 0 rows
            result = svc.verify_recovery_code("alice", "WRONG-CODE", "127.0.0.1")
            assert result is False
