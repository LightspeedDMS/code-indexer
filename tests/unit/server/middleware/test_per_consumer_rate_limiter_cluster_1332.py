"""
Tests for PR #1332 review fix: genuinely cluster-shared PerConsumerRateLimiter.

Code review rejected the original PR because PerConsumerRateLimiter built a
TokenBucketManager but never called set_connection_pool() on it -- so despite
docstring/PR claims of "one client cannot starve the fleet" and "cluster-aware
when a PG pool is attached", the limiter ran per-worker in-memory ONLY. Real
effective rate was configured x workers x nodes, not the configured rate.

This module proves:
  1. PerConsumerRateLimiter exposes set_connection_pool() and it reaches the
     underlying TokenBucketManager.
  2. The underlying manager is constructed against the DEDICATED
     consumer_rate_limit_state table (never token_bucket_state).
  3. Two limiter instances sharing one pool (simulating two nodes/workers)
     enforce ONE combined bucket -- the fleet-wide guarantee the PR claimed.

Uses real SQLite as a stand-in for PostgreSQL (established pattern in this
codebase, see test_token_bucket_pg.py) -- no mocking of the limiter itself.
"""

import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

from code_indexer.server.middleware.admission_control import PerConsumerRateLimiter


def _translate_sql(sql: str) -> str:
    sql = sql.replace("%s", "?")
    sql = sql.replace("EXCLUDED.", "excluded.")
    sql = sql.replace("LEAST(", "MIN(")
    sql = sql.replace("GREATEST(", "MAX(")
    return sql


class FakeCursor:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sqlite_cursor: Optional[sqlite3.Cursor] = None

    def execute(self, sql: str, params=None) -> "FakeCursor":
        sql = _translate_sql(sql)
        if params is not None:
            self._sqlite_cursor = self._conn.execute(sql, params)
        else:
            self._sqlite_cursor = self._conn.execute(sql)
        return self

    @property
    def rowcount(self) -> int:
        if self._sqlite_cursor is None:
            return -1
        return self._sqlite_cursor.rowcount

    def fetchone(self):
        if self._sqlite_cursor is None:
            return None
        return self._sqlite_cursor.fetchone()

    def fetchall(self):
        if self._sqlite_cursor is None:
            return []
        return self._sqlite_cursor.fetchall()

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args) -> None:
        pass


class FakeConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params=None) -> FakeCursor:
        return FakeCursor(self._conn).execute(sql, params)

    def cursor(self, row_factory=None) -> FakeCursor:
        return FakeCursor(self._conn)

    def commit(self):
        self._conn.commit()


class FakePool:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Generator[FakeConnection, None, None]:
        yield FakeConnection(self._conn)


class _Req:
    """Minimal request stand-in matching what PerConsumerRateLimiter.check() reads."""

    def __init__(self, auth: str = "Bearer shared-node-credential"):
        self.headers = {"authorization": auth}
        self.cookies: dict = {}


def _consumer_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS consumer_rate_limit_state (
            consumer_key TEXT PRIMARY KEY,
            tokens REAL NOT NULL DEFAULT 10.0,
            last_refill REAL NOT NULL,
            last_access REAL NOT NULL
        )
        """
    )
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
    return conn


class TestSetConnectionPoolWiring:
    def test_limiter_exposes_set_connection_pool(self) -> None:
        rl = PerConsumerRateLimiter(capacity=5, refill_per_second=1.0)
        assert hasattr(rl, "set_connection_pool")
        assert callable(rl.set_connection_pool)

    def test_set_connection_pool_reaches_underlying_manager(self) -> None:
        conn = _consumer_conn()
        pool = FakePool(conn)
        rl = PerConsumerRateLimiter(capacity=5, refill_per_second=1.0)
        rl.set_connection_pool(pool)
        assert rl._manager._pool is pool


class TestDedicatedTableConstruction:
    def test_underlying_manager_uses_consumer_rate_limit_state_table(self) -> None:
        rl = PerConsumerRateLimiter(capacity=5, refill_per_second=1.0)
        assert rl._manager._table_name == "consumer_rate_limit_state"

    def test_underlying_manager_uses_consumer_key_column(self) -> None:
        rl = PerConsumerRateLimiter(capacity=5, refill_per_second=1.0)
        assert rl._manager._key_column == "consumer_key"

    def test_check_writes_land_in_consumer_table_not_auth_table(self) -> None:
        conn = _consumer_conn()
        pool = FakePool(conn)
        rl = PerConsumerRateLimiter(capacity=5, refill_per_second=1.0)
        rl.set_connection_pool(pool)

        rl.check(_Req())

        auth_rows = conn.execute("SELECT * FROM token_bucket_state").fetchall()
        assert auth_rows == []
        consumer_rows = conn.execute(
            "SELECT * FROM consumer_rate_limit_state"
        ).fetchall()
        assert len(consumer_rows) == 1


class TestCrossNodeFleetWideSharing:
    """The exact guarantee the PR docstring claimed but never implemented:
    one abusive consumer cannot exceed the configured rate merely by having
    its requests round-robined across workers/nodes, because both instances
    consume from the SAME shared bucket row."""

    def test_two_limiter_instances_share_one_bucket_via_shared_pool(self) -> None:
        conn = _consumer_conn()
        pool = FakePool(conn)

        node1 = PerConsumerRateLimiter(
            capacity=2, refill_per_second=0.0, cleanup_seconds=3600
        )
        node1.set_connection_pool(pool)
        node2 = PerConsumerRateLimiter(
            capacity=2, refill_per_second=0.0, cleanup_seconds=3600
        )
        node2.set_connection_pool(pool)

        req = _Req()
        allowed_1, _ = node1.check(req)
        allowed_2, _ = node2.check(req)
        # Combined consumption == capacity: bucket exhausted after 2 total,
        # regardless of which "node" served each request.
        allowed_3, _ = node1.check(req)

        assert allowed_1 is True
        assert allowed_2 is True
        assert allowed_3 is False  # would be True if each node had its own bucket

    def test_without_shared_pool_each_instance_has_independent_bucket(self) -> None:
        """Sanity check on the OLD (broken) behaviour: no pool attached means
        each instance/worker gets its own in-memory bucket -- this is exactly
        the bug the PG wiring fixes."""
        node1 = PerConsumerRateLimiter(capacity=1, refill_per_second=0.0)
        node2 = PerConsumerRateLimiter(capacity=1, refill_per_second=0.0)

        req = _Req()
        allowed_1, _ = node1.check(req)
        allowed_2, _ = node2.check(req)

        assert allowed_1 is True
        assert allowed_2 is True  # independent bucket -- NOT fleet-wide
