"""
Tests for Task #26: TokenBucketManager PostgreSQL cluster support.

Validates that TokenBucketManager.set_connection_pool() enables PG-backed
atomic token state for cross-node rate limiting in cluster mode.

Uses real SQLite as a stand-in for PostgreSQL to avoid mocking.
PG methods use %s placeholders translated to ? for SQLite compat via FakePool.
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

import pytest


# ---------------------------------------------------------------------------
# SQLite/FakePool helpers (same pattern as test_rate_limiter_cluster.py)
# ---------------------------------------------------------------------------


def _translate_sql(sql: str) -> str:
    """Translate PG-style SQL to SQLite equivalents."""
    sql = sql.replace("%s", "?")
    sql = sql.replace("EXCLUDED.", "excluded.")
    sql = sql.replace("LEAST(", "MIN(")
    sql = sql.replace("GREATEST(", "MAX(")
    return sql


class FakeCursor:
    """Context-manager cursor backed by a sqlite3.Connection."""

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
        """Return a context-manager cursor (row_factory ignored; SQLite returns tuples)."""
        return FakeCursor(self._conn)

    def commit(self):
        self._conn.commit()


class FakePool:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Generator[FakeConnection, None, None]:
        yield FakeConnection(self._conn)


def _create_token_bucket_table(conn: sqlite3.Connection) -> None:
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_conn():
    conn = sqlite3.connect(":memory:")
    _create_token_bucket_table(conn)
    yield conn
    conn.close()


@pytest.fixture
def pool_and_conn(sqlite_conn):
    pool = FakePool(sqlite_conn)
    return pool, sqlite_conn


def _make_manager_with_pool(
    pool_and_conn, capacity: int = 10, refill_rate: float = 1 / 6.0
):
    from code_indexer.server.auth.token_bucket import TokenBucketManager

    pool, conn = pool_and_conn
    manager = TokenBucketManager(capacity=capacity, refill_rate=refill_rate)
    manager.set_connection_pool(pool)
    return manager, conn, pool


def _make_two_managers_with_shared_pool(
    sqlite_conn, capacity: int = 2, refill_rate: float = 0.0
):
    from code_indexer.server.auth.token_bucket import TokenBucketManager

    pool = FakePool(sqlite_conn)
    node1 = TokenBucketManager(capacity=capacity, refill_rate=refill_rate)
    node1.set_connection_pool(pool)
    node2 = TokenBucketManager(capacity=capacity, refill_rate=refill_rate)
    node2.set_connection_pool(pool)
    return node1, node2


# ---------------------------------------------------------------------------
# Test: set_connection_pool stores pool and logs
# ---------------------------------------------------------------------------


class TestSetConnectionPool:
    def test_pool_is_none_by_default(self) -> None:
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        manager = TokenBucketManager()
        assert manager._pool is None

    def test_set_connection_pool_stores_pool_reference(self, pool_and_conn) -> None:
        manager, _, pool = _make_manager_with_pool(pool_and_conn)
        assert manager._pool is pool

    def test_set_connection_pool_logs_cluster_mode(self, sqlite_conn, caplog) -> None:
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        pool = FakePool(sqlite_conn)
        manager = TokenBucketManager()
        with caplog.at_level(logging.INFO):
            manager.set_connection_pool(pool)
        assert any("cluster" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Test: consume() in PG mode
# ---------------------------------------------------------------------------


class TestConsumePg:
    def test_consume_allowed_when_tokens_available(self, pool_and_conn) -> None:
        manager, _, _ = _make_manager_with_pool(pool_and_conn, capacity=10)
        allowed, retry = manager.consume("alice")
        assert allowed is True
        assert retry == 0.0

    def test_consume_inserts_row_on_first_use(self, pool_and_conn) -> None:
        manager, conn, _ = _make_manager_with_pool(pool_and_conn, capacity=10)
        manager.consume("alice")
        row = conn.execute(
            "SELECT username FROM token_bucket_state WHERE username = 'alice'"
        ).fetchone()
        assert row is not None

    def test_consume_decrements_token_count(self, pool_and_conn) -> None:
        manager, conn, _ = _make_manager_with_pool(pool_and_conn, capacity=10)
        manager.consume("alice")
        row = conn.execute(
            "SELECT tokens FROM token_bucket_state WHERE username = 'alice'"
        ).fetchone()
        assert row is not None
        assert row[0] < 10.0

    def test_consume_denied_when_no_tokens(self, pool_and_conn) -> None:
        manager, _, _ = _make_manager_with_pool(
            pool_and_conn, capacity=2, refill_rate=0.0
        )
        manager.consume("alice")
        manager.consume("alice")
        allowed, retry = manager.consume("alice")
        assert allowed is False
        assert retry > 0.0

    def test_consume_retry_after_is_positive_when_denied(self, pool_and_conn) -> None:
        manager, _, _ = _make_manager_with_pool(
            pool_and_conn, capacity=1, refill_rate=1.0
        )
        manager.consume("alice")
        allowed, retry = manager.consume("alice")
        assert not allowed
        assert retry > 0.0

    def test_consume_is_per_username(self, pool_and_conn) -> None:
        manager, _, _ = _make_manager_with_pool(
            pool_and_conn, capacity=1, refill_rate=0.0
        )
        manager.consume("alice")
        allowed_bob, _ = manager.consume("bob")
        assert allowed_bob is True


# ---------------------------------------------------------------------------
# Test: refund() in PG mode
# ---------------------------------------------------------------------------


class TestRefundPg:
    def test_refund_increases_tokens(self, pool_and_conn) -> None:
        manager, conn, _ = _make_manager_with_pool(pool_and_conn, capacity=10)
        manager.consume("alice")
        before = conn.execute(
            "SELECT tokens FROM token_bucket_state WHERE username = 'alice'"
        ).fetchone()[0]
        manager.refund("alice")
        after = conn.execute(
            "SELECT tokens FROM token_bucket_state WHERE username = 'alice'"
        ).fetchone()[0]
        assert after > before

    def test_refund_does_not_exceed_capacity(self, pool_and_conn) -> None:
        manager, conn, _ = _make_manager_with_pool(pool_and_conn, capacity=10)
        manager.consume("alice")
        for _ in range(20):
            manager.refund("alice")
        row = conn.execute(
            "SELECT tokens FROM token_bucket_state WHERE username = 'alice'"
        ).fetchone()
        assert row[0] <= 10.0

    def test_refund_then_consume_via_different_manager_uses_pool(
        self, sqlite_conn
    ) -> None:
        node1, node2 = _make_two_managers_with_shared_pool(
            sqlite_conn, capacity=1, refill_rate=0.0
        )
        node1.consume("alice")
        allowed_before_refund, _ = node2.consume("alice")
        assert allowed_before_refund is False
        node1.refund("alice")
        allowed_after_refund, _ = node2.consume("alice")
        assert allowed_after_refund is True


# ---------------------------------------------------------------------------
# Test: cross-node sharing
# ---------------------------------------------------------------------------


class TestCrossNodeSharing:
    def test_two_managers_share_pg_state(self, sqlite_conn) -> None:
        node1, node2 = _make_two_managers_with_shared_pool(
            sqlite_conn, capacity=2, refill_rate=0.0
        )
        node1.consume("alice")
        node1.consume("alice")
        allowed, _ = node2.consume("alice")
        assert allowed is False

    def test_different_usernames_do_not_interfere(self, sqlite_conn) -> None:
        node1, node2 = _make_two_managers_with_shared_pool(
            sqlite_conn, capacity=1, refill_rate=0.0
        )
        node1.consume("alice")
        allowed_bob, _ = node2.consume("bob")
        assert allowed_bob is True


# ---------------------------------------------------------------------------
# Test: standalone (in-memory) mode still works
# ---------------------------------------------------------------------------


class TestStandaloneMode:
    def test_consume_works_without_pool(self) -> None:
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        manager = TokenBucketManager(capacity=5)
        allowed, retry = manager.consume("alice")
        assert allowed is True
        assert retry == 0.0

    def test_refund_works_without_pool(self) -> None:
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        manager = TokenBucketManager(capacity=2)
        manager.consume("alice")
        manager.refund("alice")
        allowed, _ = manager.consume("alice")
        assert allowed is True

    def test_standalone_depletes_to_zero(self) -> None:
        from code_indexer.server.auth.token_bucket import TokenBucketManager

        manager = TokenBucketManager(capacity=2, refill_rate=0.0)
        manager.consume("alice")
        manager.consume("alice")
        allowed, retry = manager.consume("alice")
        assert allowed is False
        assert retry > 0.0


# ---------------------------------------------------------------------------
# Test: module-level singleton contract
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_rate_limiter_singleton_has_set_connection_pool(self) -> None:
        from code_indexer.server.auth.token_bucket import rate_limiter

        assert hasattr(rate_limiter, "set_connection_pool")
        assert callable(rate_limiter.set_connection_pool)

    def test_rate_limiter_singleton_pool_is_none_at_module_load(self) -> None:
        from code_indexer.server.auth.token_bucket import rate_limiter

        assert rate_limiter._pool is None


# ---------------------------------------------------------------------------
# Test: lifespan wires rate_limiter to cluster pool (structural)
# ---------------------------------------------------------------------------


class TestLifespanWiring:
    def test_lifespan_imports_rate_limiter_from_token_bucket(self) -> None:
        import inspect

        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)
        assert "token_bucket" in source

    def test_lifespan_calls_rate_limiter_set_connection_pool(self) -> None:
        import inspect

        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)
        assert "rate_limiter" in source
        assert "set_connection_pool" in source
