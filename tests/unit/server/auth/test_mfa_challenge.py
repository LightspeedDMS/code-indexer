"""
Tests for MFA Challenge Token Manager (Story #560).

Tests challenge creation, retrieval, expiry, attempt exhaustion,
consumption, IP validation, and TTL enforcement in consume.
Cluster-mode (PostgreSQL) tests added for C3.
"""

import sqlite3
import time

import pytest

from code_indexer.server.auth.mfa_challenge import MfaChallengeManager


@pytest.fixture
def manager():
    return MfaChallengeManager(ttl_seconds=300, max_attempts=5)


# ------------------------------------------------------------------
# Cluster/PostgreSQL mode test infrastructure (C3)
# ------------------------------------------------------------------


class _PgStyleSqliteConn:
    """SQLite connection presenting psycopg-style interface.

    Translates %s placeholders to ? for SQLite compatibility.
    """

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._conn.row_factory = sqlite3.Row

    @staticmethod
    def _translate_query(query):
        """Replace %s placeholders with ? for SQLite."""
        return query.replace("%s", "?")

    def execute(self, query, params=None):
        translated = self._translate_query(query)
        if params:
            return self._conn.execute(translated, params)
        return self._conn.execute(translated)

    def commit(self):
        self._conn.commit()

    def close(self):
        pass


class _PgStyleSqlitePoolCtx:
    """Context manager for pool.connection()."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return _PgStyleSqliteConn(self._conn)

    def __exit__(self, *args):
        pass


class _PgStyleSqlitePool:
    """SQLite-backed pool presenting psycopg v3 ConnectionPool interface."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mfa_challenges (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                client_ip TEXT NOT NULL,
                redirect_url TEXT DEFAULT '/admin/',
                created_at REAL NOT NULL,
                attempt_count INTEGER DEFAULT 0,
                oauth_client_id TEXT,
                oauth_redirect_uri TEXT,
                oauth_code_challenge TEXT,
                oauth_state TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mfa_challenges_created
            ON mfa_challenges(created_at);
            """
        )
        self._conn.commit()

    def connection(self):
        return _PgStyleSqlitePoolCtx(self._conn)

    def close(self):
        self._conn.close()


@pytest.fixture
def pg_pool():
    """Create a PG-style SQLite pool for cluster mode tests."""
    pool = _PgStyleSqlitePool()
    yield pool
    pool.close()


@pytest.fixture
def manager_with_pool(pg_pool):
    """Create MfaChallengeManager with pool set (cluster mode)."""
    mgr = MfaChallengeManager(ttl_seconds=300, max_attempts=5)
    mgr.set_connection_pool(pg_pool)
    return mgr


class TestCreateChallenge:
    def test_returns_token_string(self, manager):
        token = manager.create_challenge("admin", "admin", "127.0.0.1")
        assert isinstance(token, str)
        assert len(token) > 20

    def test_stores_username_role_and_ip(self, manager):
        token = manager.create_challenge(
            "admin", "power_user", "10.0.0.1", redirect_url="/dash"
        )
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.username == "admin"
        assert challenge.role == "power_user"
        assert challenge.client_ip == "10.0.0.1"
        assert challenge.redirect_url == "/dash"


class TestGetChallenge:
    def test_returns_valid_challenge(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.username == "alice"

    def test_returns_none_for_unknown_token(self, manager):
        assert manager.get_challenge("nonexistent") is None

    def test_returns_none_for_expired_challenge(self):
        mgr = MfaChallengeManager(ttl_seconds=1, max_attempts=5)
        token = mgr.create_challenge("alice", "admin", "127.0.0.1")
        time.sleep(1.1)
        assert mgr.get_challenge(token) is None

    def test_returns_none_when_attempts_exhausted(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        for _ in range(5):
            manager.record_attempt(token)
        assert manager.get_challenge(token) is None

    def test_ip_mismatch_returns_none(self, manager):
        token = manager.create_challenge("alice", "admin", "10.0.0.1")
        # Same IP passes
        assert manager.get_challenge(token, client_ip="10.0.0.1") is not None
        # Different IP fails
        assert manager.get_challenge(token, client_ip="192.168.1.1") is None

    def test_ip_none_skips_validation(self, manager):
        token = manager.create_challenge("alice", "admin", "10.0.0.1")
        # No IP = no validation
        assert manager.get_challenge(token, client_ip=None) is not None


class TestRecordAttempt:
    def test_increments_counter(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        manager.record_attempt(token)
        challenge = manager.get_challenge(token)
        assert challenge is not None
        assert challenge.attempt_count == 1

    def test_noop_for_unknown_token(self, manager):
        manager.record_attempt("nonexistent")  # Should not raise


class TestConsume:
    def test_removes_and_returns_challenge(self, manager):
        token = manager.create_challenge("alice", "admin", "127.0.0.1")
        challenge = manager.consume(token)
        assert challenge is not None
        assert challenge.username == "alice"
        assert challenge.role == "admin"
        assert manager.get_challenge(token) is None

    def test_returns_none_for_unknown_token(self, manager):
        assert manager.consume("nonexistent") is None

    def test_returns_none_for_expired_challenge(self):
        """consume() must enforce TTL — expired tokens return None."""
        mgr = MfaChallengeManager(ttl_seconds=1, max_attempts=5)
        token = mgr.create_challenge("alice", "admin", "127.0.0.1")
        time.sleep(1.1)
        assert mgr.consume(token) is None


# ------------------------------------------------------------------
# Cluster/PostgreSQL mode tests (C3)
# ------------------------------------------------------------------


class TestClusterMode:
    def test_create_challenge_via_pool(self, manager_with_pool, pg_pool):
        """create_challenge must INSERT into PostgreSQL when pool is set."""
        token = manager_with_pool.create_challenge(
            "admin", "admin", "10.0.0.1", redirect_url="/dashboard"
        )
        assert isinstance(token, str)
        assert len(token) > 20

        # Verify row exists in DB
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT username, role, client_ip, redirect_url, attempt_count "
                "FROM mfa_challenges WHERE token = %s",
                (token,),
            ).fetchone()
        assert row is not None
        assert row["username"] == "admin"
        assert row["role"] == "admin"
        assert row["client_ip"] == "10.0.0.1"
        assert row["redirect_url"] == "/dashboard"
        assert row["attempt_count"] == 0

    def test_get_challenge_via_pool(self, manager_with_pool):
        """get_challenge must SELECT from PostgreSQL when pool is set."""
        token = manager_with_pool.create_challenge("alice", "power_user", "10.0.0.1")
        challenge = manager_with_pool.get_challenge(token, client_ip="10.0.0.1")
        assert challenge is not None
        assert challenge.username == "alice"
        assert challenge.role == "power_user"
        assert challenge.client_ip == "10.0.0.1"

        # IP mismatch returns None
        assert manager_with_pool.get_challenge(token, client_ip="99.99.99.99") is None

    def test_consume_via_pool(self, manager_with_pool, pg_pool):
        """consume must DELETE from PostgreSQL and return challenge data."""
        token = manager_with_pool.create_challenge("bob", "admin", "127.0.0.1")
        challenge = manager_with_pool.consume(token)
        assert challenge is not None
        assert challenge.username == "bob"
        assert challenge.role == "admin"

        # Verify row removed from DB
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT token FROM mfa_challenges WHERE token = %s",
                (token,),
            ).fetchone()
        assert row is None

        # Second consume returns None
        assert manager_with_pool.consume(token) is None

    def test_record_attempt_via_pool(self, manager_with_pool, pg_pool):
        """record_attempt must UPDATE attempt_count in PostgreSQL."""
        token = manager_with_pool.create_challenge("carol", "admin", "127.0.0.1")
        manager_with_pool.record_attempt(token)
        manager_with_pool.record_attempt(token)

        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM mfa_challenges WHERE token = %s",
                (token,),
            ).fetchone()
        assert row is not None
        assert row["attempt_count"] == 2

    def test_expired_challenge_cleaned_via_pool(self, pg_pool):
        """_cleanup_expired must DELETE old rows from PostgreSQL."""
        mgr = MfaChallengeManager(ttl_seconds=1, max_attempts=5)
        mgr.set_connection_pool(pg_pool)
        token = mgr.create_challenge("dave", "admin", "127.0.0.1")
        time.sleep(1.1)

        # get_challenge triggers cleanup and returns None for expired
        assert mgr.get_challenge(token) is None

        # Verify row removed from DB
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT token FROM mfa_challenges WHERE token = %s",
                (token,),
            ).fetchone()
        assert row is None
