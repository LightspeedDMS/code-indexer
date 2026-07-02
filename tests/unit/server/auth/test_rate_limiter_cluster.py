"""
Tests for Bug #573/#574: Cluster-mode rate limiting for all 4 rate limiters.

Validates that PasswordChangeRateLimiter, RefreshTokenRateLimiter,
OAuthTokenRateLimiter, and OAuthRegisterRateLimiter all support
PostgreSQL via set_connection_pool() for cross-node lockout.

Uses real SQLite as a stand-in for PostgreSQL to avoid mocking.
The PG methods use %s placeholders which we adapt via a thin wrapper.
"""

import sqlite3
from contextlib import contextmanager
from typing import Generator


class FakeCursor:
    """Wraps a sqlite3.Cursor to provide fetchone/fetchall."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class FakeConnection:
    """Wraps a sqlite3.Connection to translate %s -> ? for SQLite compat."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params=None):
        # Translate PG-style %s placeholders to SQLite ? placeholders
        sql = sql.replace("%s", "?")
        # Translate ON CONFLICT ... DO UPDATE SET x = EXCLUDED.x
        # to SQLite-compatible ON CONFLICT ... DO UPDATE SET x = excluded.x
        sql = sql.replace("EXCLUDED.", "excluded.")
        if params:
            return FakeCursor(self._conn.execute(sql, params))
        return FakeCursor(self._conn.execute(sql))

    def commit(self):
        self._conn.commit()


class FakePool:
    """Fake connection pool that wraps a shared SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Generator[FakeConnection, None, None]:
        yield FakeConnection(self._conn)


def _create_rate_limit_tables(conn: sqlite3.Connection) -> None:
    """Create the generic rate_limit_failures and rate_limit_lockouts tables."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limit_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            limiter_type TEXT NOT NULL,
            identifier TEXT NOT NULL,
            failed_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rate_limit_failures_lookup
        ON rate_limit_failures(limiter_type, identifier, failed_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limit_lockouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            limiter_type TEXT NOT NULL,
            identifier TEXT NOT NULL,
            locked_until REAL NOT NULL,
            UNIQUE(limiter_type, identifier)
        )
        """
    )
    conn.commit()


class TestPasswordChangeRateLimiterCluster:
    """Bug #573: PasswordChangeRateLimiter cluster mode via set_connection_pool."""

    def _make_limiter_with_pool(self):
        from code_indexer.server.auth.rate_limiter import PasswordChangeRateLimiter

        conn = sqlite3.connect(":memory:")
        _create_rate_limit_tables(conn)
        pool = FakePool(conn)
        limiter = PasswordChangeRateLimiter()
        limiter.set_connection_pool(pool)
        return limiter, conn

    def test_set_connection_pool_stores_pool(self) -> None:
        """set_connection_pool must store pool reference."""
        limiter, _ = self._make_limiter_with_pool()
        assert limiter._pool is not None

    def test_set_connection_pool_sets_limiter_type(self) -> None:
        """set_connection_pool must set _limiter_type to 'password_change'."""
        limiter, _ = self._make_limiter_with_pool()
        assert limiter._limiter_type == "password_change"

    def test_failure_tracked_via_pg(self) -> None:
        """record_failed_attempt must insert into rate_limit_failures table."""
        limiter, conn = self._make_limiter_with_pool()
        limiter.record_failed_attempt("alice")
        row = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_failures "
            "WHERE limiter_type = 'password_change' AND identifier = 'alice'"
        ).fetchone()
        assert row[0] == 1

    def test_lockout_enforced_via_pg(self) -> None:
        """After max_attempts failures, check_rate_limit must return lockout message."""
        limiter, _ = self._make_limiter_with_pool()
        for _ in range(5):
            limiter.record_failed_attempt("alice")
        msg = limiter.check_rate_limit("alice")
        assert msg is not None
        assert "Too many failed attempts" in msg

    def test_no_lockout_before_max_attempts(self) -> None:
        """Before max_attempts, check_rate_limit must return None."""
        limiter, _ = self._make_limiter_with_pool()
        for _ in range(4):
            limiter.record_failed_attempt("alice")
        msg = limiter.check_rate_limit("alice")
        assert msg is None

    def test_success_clears_via_pg(self) -> None:
        """record_successful_attempt must clear failures and lockouts."""
        limiter, conn = self._make_limiter_with_pool()
        for _ in range(5):
            limiter.record_failed_attempt("alice")
        limiter.record_successful_attempt("alice")
        msg = limiter.check_rate_limit("alice")
        assert msg is None
        row = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_failures "
            "WHERE limiter_type = 'password_change' AND identifier = 'alice'"
        ).fetchone()
        assert row[0] == 0

    def test_record_failed_attempt_returns_true_on_lockout(self) -> None:
        """record_failed_attempt must return True when lockout triggered."""
        limiter, _ = self._make_limiter_with_pool()
        results = []
        for _ in range(5):
            results.append(limiter.record_failed_attempt("alice"))
        assert results[-1] is True
        assert all(r is False for r in results[:-1])


class TestRefreshTokenRateLimiterCluster:
    """Bug #573: RefreshTokenRateLimiter inherits PG support, different limiter_type."""

    def _make_limiter_with_pool(self):
        from code_indexer.server.auth.rate_limiter import RefreshTokenRateLimiter

        conn = sqlite3.connect(":memory:")
        _create_rate_limit_tables(conn)
        pool = FakePool(conn)
        limiter = RefreshTokenRateLimiter()
        limiter.set_connection_pool(pool)
        return limiter, conn

    def test_uses_different_limiter_type(self) -> None:
        """RefreshTokenRateLimiter must use limiter_type='refresh_token'."""
        limiter, _ = self._make_limiter_with_pool()
        assert limiter._limiter_type == "refresh_token"

    def test_failure_tracked_with_refresh_token_type(self) -> None:
        """Failures must be stored with limiter_type='refresh_token'."""
        limiter, conn = self._make_limiter_with_pool()
        limiter.record_failed_attempt("bob")
        row = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_failures "
            "WHERE limiter_type = 'refresh_token' AND identifier = 'bob'"
        ).fetchone()
        assert row[0] == 1

    def test_lockout_after_10_attempts(self) -> None:
        """RefreshTokenRateLimiter locks out after 10 attempts (not 5)."""
        limiter, _ = self._make_limiter_with_pool()
        for _ in range(9):
            limiter.record_failed_attempt("bob")
        assert limiter.check_rate_limit("bob") is None
        limiter.record_failed_attempt("bob")
        msg = limiter.check_rate_limit("bob")
        assert msg is not None
        assert "Too many failed attempts" in msg


class TestOAuthTokenRateLimiterCluster:
    """Bug #574: OAuthTokenRateLimiter cluster mode."""

    def _make_limiter_with_pool(self):
        from code_indexer.server.auth.oauth_rate_limiter import OAuthTokenRateLimiter

        conn = sqlite3.connect(":memory:")
        _create_rate_limit_tables(conn)
        pool = FakePool(conn)
        limiter = OAuthTokenRateLimiter()
        limiter.set_connection_pool(pool)
        return limiter, conn

    def test_set_connection_pool_stores_pool(self) -> None:
        """set_connection_pool must store pool reference."""
        limiter, _ = self._make_limiter_with_pool()
        assert limiter._pool is not None

    def test_limiter_type_is_oauth_token(self) -> None:
        """Must use limiter_type='oauth_token'."""
        limiter, _ = self._make_limiter_with_pool()
        assert limiter._limiter_type == "oauth_token"

    def test_failure_tracked_via_pg(self) -> None:
        """record_failed_attempt must insert into rate_limit_failures."""
        limiter, conn = self._make_limiter_with_pool()
        limiter.record_failed_attempt("client-xyz")
        row = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_failures "
            "WHERE limiter_type = 'oauth_token' AND identifier = 'client-xyz'"
        ).fetchone()
        assert row[0] == 1

    def test_lockout_after_max_attempts(self) -> None:
        """OAuthTokenRateLimiter locks out after 10 attempts."""
        limiter, _ = self._make_limiter_with_pool()
        for _ in range(10):
            limiter.record_failed_attempt("client-xyz")
        msg = limiter.check_rate_limit("client-xyz")
        assert msg is not None
        assert "Too many failed attempts" in msg

    def test_success_clears_via_pg(self) -> None:
        """record_successful_attempt must clear failures and lockouts."""
        limiter, conn = self._make_limiter_with_pool()
        for _ in range(10):
            limiter.record_failed_attempt("client-xyz")
        limiter.record_successful_attempt("client-xyz")
        assert limiter.check_rate_limit("client-xyz") is None


class TestOAuthRegisterRateLimiterCluster:
    """Bug #574: OAuthRegisterRateLimiter cluster mode."""

    def _make_limiter_with_pool(self):
        from code_indexer.server.auth.oauth_rate_limiter import (
            OAuthRegisterRateLimiter,
        )

        conn = sqlite3.connect(":memory:")
        _create_rate_limit_tables(conn)
        pool = FakePool(conn)
        limiter = OAuthRegisterRateLimiter()
        limiter.set_connection_pool(pool)
        return limiter, conn

    def test_set_connection_pool_stores_pool(self) -> None:
        """set_connection_pool must store pool reference."""
        limiter, _ = self._make_limiter_with_pool()
        assert limiter._pool is not None

    def test_limiter_type_is_oauth_register(self) -> None:
        """Must use limiter_type='oauth_register'."""
        limiter, _ = self._make_limiter_with_pool()
        assert limiter._limiter_type == "oauth_register"

    def test_failure_tracked_via_pg(self) -> None:
        """record_failed_attempt must insert into rate_limit_failures."""
        limiter, conn = self._make_limiter_with_pool()
        limiter.record_failed_attempt("192.168.1.100")
        row = conn.execute(
            "SELECT COUNT(*) FROM rate_limit_failures "
            "WHERE limiter_type = 'oauth_register' AND identifier = '192.168.1.100'"
        ).fetchone()
        assert row[0] == 1

    def test_lockout_after_5_attempts(self) -> None:
        """OAuthRegisterRateLimiter locks out after 5 attempts."""
        limiter, _ = self._make_limiter_with_pool()
        for _ in range(5):
            limiter.record_failed_attempt("192.168.1.100")
        msg = limiter.check_rate_limit("192.168.1.100")
        assert msg is not None
        assert "Too many failed attempts" in msg

    def test_success_clears_via_pg(self) -> None:
        """record_successful_attempt must clear failures and lockouts."""
        limiter, conn = self._make_limiter_with_pool()
        for _ in range(5):
            limiter.record_failed_attempt("192.168.1.100")
        limiter.record_successful_attempt("192.168.1.100")
        assert limiter.check_rate_limit("192.168.1.100") is None


class TestCrossNodeLockout:
    """Verify that failures on one 'node' enforce lockout on another."""

    def test_cross_node_lockout_password_change(self) -> None:
        """Two PasswordChangeRateLimiter instances sharing same PG
        must enforce lockout across nodes."""
        from code_indexer.server.auth.rate_limiter import PasswordChangeRateLimiter

        conn = sqlite3.connect(":memory:")
        _create_rate_limit_tables(conn)
        pool = FakePool(conn)

        node1 = PasswordChangeRateLimiter()
        node1.set_connection_pool(pool)
        node2 = PasswordChangeRateLimiter()
        node2.set_connection_pool(pool)

        # Record 5 failures on node1
        for _ in range(5):
            node1.record_failed_attempt("alice")

        # node2 must see the lockout
        msg = node2.check_rate_limit("alice")
        assert msg is not None
        assert "Too many failed attempts" in msg

    def test_cross_node_lockout_oauth_token(self) -> None:
        """Two OAuthTokenRateLimiter instances sharing same PG
        must enforce lockout across nodes."""
        from code_indexer.server.auth.oauth_rate_limiter import OAuthTokenRateLimiter

        conn = sqlite3.connect(":memory:")
        _create_rate_limit_tables(conn)
        pool = FakePool(conn)

        node1 = OAuthTokenRateLimiter()
        node1.set_connection_pool(pool)
        node2 = OAuthTokenRateLimiter()
        node2.set_connection_pool(pool)

        for _ in range(10):
            node1.record_failed_attempt("client-abc")

        msg = node2.check_rate_limit("client-abc")
        assert msg is not None
        assert "Too many failed attempts" in msg

    def test_different_limiter_types_isolated(self) -> None:
        """Different limiter_types must NOT interfere with each other."""
        from code_indexer.server.auth.rate_limiter import (
            PasswordChangeRateLimiter,
            RefreshTokenRateLimiter,
        )

        conn = sqlite3.connect(":memory:")
        _create_rate_limit_tables(conn)
        pool = FakePool(conn)

        pw_limiter = PasswordChangeRateLimiter()
        pw_limiter.set_connection_pool(pool)
        rt_limiter = RefreshTokenRateLimiter()
        rt_limiter.set_connection_pool(pool)

        # Lock out password_change for alice
        for _ in range(5):
            pw_limiter.record_failed_attempt("alice")

        # refresh_token for alice must NOT be locked
        msg = rt_limiter.check_rate_limit("alice")
        assert msg is None


class TestLifespanWiringStructural:
    """Verify lifespan.py wires cluster pools to all rate limiters."""

    def test_lifespan_wires_password_change_rate_limiter(self) -> None:
        """lifespan.py must import and wire password_change_rate_limiter."""
        import inspect
        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)
        assert "password_change_rate_limiter" in source

    def test_lifespan_wires_refresh_token_rate_limiter(self) -> None:
        """lifespan.py must import and wire refresh_token_rate_limiter."""
        import inspect
        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)
        assert "refresh_token_rate_limiter" in source

    def test_lifespan_wires_oauth_token_rate_limiter(self) -> None:
        """lifespan.py must import and wire oauth_token_rate_limiter."""
        import inspect
        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)
        assert "oauth_token_rate_limiter" in source

    def test_lifespan_wires_oauth_register_rate_limiter(self) -> None:
        """lifespan.py must import and wire oauth_register_rate_limiter."""
        import inspect
        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)
        assert "oauth_register_rate_limiter" in source
