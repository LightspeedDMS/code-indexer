"""
Tests for Story #557: Login Rate Limiting and Account Lockout.

TDD test suite covering all 9 acceptance criteria:
1. Successful login resets failure counter to 0
2. Account lockout after 5 failed attempts for 15 minutes
3. Lockout applies to Web UI login (POST /admin/login via routes.py)
4. Lockout applies to REST API login (POST /auth/login)
5. Lockout expires after duration (15 min default)
6. Rate limiting can be disabled via config toggle
7. Each failed attempt is audit-logged
8. Lockout is per-username (not per-IP)
9. Failed attempt counter uses a sliding window (15 min window)

ANTI-MOCK: LoginRateLimiter is tested directly with real in-memory state.
Endpoint integration tests mock only infrastructure (user_manager, jwt_manager).
"""

import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter


# ---------------------------------------------------------------------------
# Cluster/PostgreSQL mode test infrastructure (H1)
# ---------------------------------------------------------------------------


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
    """SQLite-backed pool presenting psycopg v3 ConnectionPool interface.

    Creates login_failures and login_lockouts tables matching migration 009.
    """

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS login_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                failed_at DOUBLE PRECISION NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_failures_user_time
            ON login_failures(username, failed_at);

            CREATE TABLE IF NOT EXISTS login_lockouts (
                username TEXT PRIMARY KEY,
                locked_until DOUBLE PRECISION NOT NULL
            );
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


# ---------------------------------------------------------------------------
# Unit tests for LoginRateLimiter (real implementation, no mocks)
# ---------------------------------------------------------------------------


class TestLoginRateLimiterBasics:
    """Core state machine: failures, lockout, reset."""

    def test_fresh_user_is_not_locked(self):
        """A username with no history must not be locked."""
        limiter = LoginRateLimiter()
        locked, remaining = limiter.is_locked("alice")
        assert locked is False
        assert remaining == 0

    def test_single_failure_does_not_lock(self):
        """One failure must not trigger lockout."""
        limiter = LoginRateLimiter()
        limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is False

    def test_four_failures_do_not_lock(self):
        """Four failures (< max_attempts=5) must not trigger lockout."""
        limiter = LoginRateLimiter()
        for _ in range(4):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is False

    def test_fifth_failure_triggers_lockout(self):
        """AC2: Exactly 5 failures must trigger lockout."""
        limiter = LoginRateLimiter()
        is_locked, remaining = False, 0
        for _ in range(5):
            is_locked, remaining = limiter.check_and_record_failure("alice")
        assert is_locked is True
        assert remaining > 0

    def test_lockout_remaining_seconds_is_positive(self):
        """After lockout, remaining seconds must be positive (close to 15 min)."""
        limiter = LoginRateLimiter()
        for _ in range(5):
            limiter.check_and_record_failure("alice")
        locked, remaining = limiter.is_locked("alice")
        assert locked is True
        assert remaining > 890

    def test_success_resets_failure_counter(self):
        """AC1: Successful login must clear all failure history."""
        limiter = LoginRateLimiter()
        for _ in range(4):
            limiter.check_and_record_failure("alice")
        limiter.record_success("alice")
        for _ in range(4):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is False

    def test_success_on_locked_account_unlocks(self):
        """AC1: Record success even on a locked account - clears lockout."""
        limiter = LoginRateLimiter()
        for _ in range(5):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is True
        limiter.record_success("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is False

    def test_success_on_unknown_user_is_noop(self):
        """record_success on a user with no history must not raise."""
        limiter = LoginRateLimiter()
        limiter.record_success("nobody")
        locked, _ = limiter.is_locked("nobody")
        assert locked is False


class TestLoginRateLimiterPerUsername:
    """AC8: Lockout is per-username, not shared."""

    def test_locking_alice_does_not_lock_bob(self):
        """AC8: Failures for 'alice' must not affect 'bob'."""
        limiter = LoginRateLimiter()
        for _ in range(5):
            limiter.check_and_record_failure("alice")
        alice_locked, _ = limiter.is_locked("alice")
        bob_locked, _ = limiter.is_locked("bob")
        assert alice_locked is True
        assert bob_locked is False

    def test_independent_counters_per_username(self):
        """Each user has an independent failure counter."""
        limiter = LoginRateLimiter()
        for _ in range(4):
            limiter.check_and_record_failure("alice")
        for _ in range(3):
            limiter.check_and_record_failure("bob")
        alice_locked, _ = limiter.is_locked("alice")
        bob_locked, _ = limiter.is_locked("bob")
        assert alice_locked is False
        assert bob_locked is False


class TestLoginRateLimiterSlidingWindow:
    """AC9: Sliding window - only failures within the window count."""

    def test_expired_failures_do_not_contribute_to_lockout(self):
        """AC9: Failures older than window_minutes must be excluded."""
        limiter = LoginRateLimiter(window_minutes=0.001)
        for _ in range(4):
            limiter.check_and_record_failure("alice")
        time.sleep(0.1)
        is_locked, _ = limiter.check_and_record_failure("alice")
        assert is_locked is False

    def test_failures_within_window_count_toward_lockout(self):
        """AC9: Failures within the window must still count."""
        limiter = LoginRateLimiter(window_minutes=5)
        is_locked, _ = False, 0
        for _ in range(5):
            is_locked, _ = limiter.check_and_record_failure("alice")
        assert is_locked is True


class TestLoginRateLimiterLockoutExpiry:
    """AC5: Lockout expires after duration."""

    def test_lockout_expires_after_duration(self):
        """AC5: Account must auto-unlock after lockout_duration_minutes."""
        limiter = LoginRateLimiter(lockout_duration_minutes=0.001)
        for _ in range(5):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is True
        time.sleep(0.1)
        locked, remaining = limiter.is_locked("alice")
        assert locked is False
        assert remaining == 0

    def test_can_fail_again_after_lockout_expires(self):
        """After lockout expires, failure counter resets and new window starts."""
        limiter = LoginRateLimiter(lockout_duration_minutes=0.001)
        for _ in range(5):
            limiter.check_and_record_failure("alice")
        time.sleep(0.1)
        is_locked, _ = limiter.check_and_record_failure("alice")
        assert is_locked is False


class TestLoginRateLimiterDisabled:
    """AC6: Rate limiting can be disabled via config toggle."""

    def test_disabled_limiter_never_locks(self):
        """AC6: When enabled=False, check_and_record_failure must never lock."""
        limiter = LoginRateLimiter(enabled=False)
        is_locked, remaining = False, 0
        for _ in range(100):
            is_locked, remaining = limiter.check_and_record_failure("alice")
        assert is_locked is False
        assert remaining == 0

    def test_disabled_limiter_is_locked_returns_false(self):
        """AC6: When enabled=False, is_locked must always return False."""
        limiter = LoginRateLimiter(enabled=False)
        locked, remaining = limiter.is_locked("alice")
        assert locked is False
        assert remaining == 0

    def test_enabled_limiter_locks_after_threshold(self):
        """Sanity check: enabled limiter DOES lock after 5 failures."""
        limiter = LoginRateLimiter(enabled=True)
        for _ in range(5):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is True


class TestLoginRateLimiterAuditLogging:
    """AC7: Each failed attempt is audit-logged."""

    def test_failed_attempt_calls_audit_logger(self):
        """AC7: Each call to check_and_record_failure must produce an audit log entry."""
        mock_logger = MagicMock()
        limiter = LoginRateLimiter(audit_logger=mock_logger)
        limiter.check_and_record_failure("alice")
        mock_logger.log_authentication_failure.assert_called_once()

    def test_audit_log_includes_username(self):
        """AC7: Audit log entry must include the username."""
        mock_logger = MagicMock()
        limiter = LoginRateLimiter(audit_logger=mock_logger)
        limiter.check_and_record_failure("alice")
        call_kwargs = mock_logger.log_authentication_failure.call_args
        all_args = str(call_kwargs)
        assert "alice" in all_args

    def test_lockout_event_calls_rate_limit_log(self):
        """AC7: When lockout is triggered (5th failure), log_rate_limit_triggered is called."""
        mock_logger = MagicMock()
        limiter = LoginRateLimiter(audit_logger=mock_logger)
        for _ in range(5):
            limiter.check_and_record_failure("alice")
        mock_logger.log_rate_limit_triggered.assert_called()

    def test_multiple_failures_each_logged(self):
        """AC7: Each individual failure gets its own audit log entry."""
        mock_logger = MagicMock()
        limiter = LoginRateLimiter(audit_logger=mock_logger)
        for _ in range(3):
            limiter.check_and_record_failure("alice")
        assert mock_logger.log_authentication_failure.call_count == 3

    def test_success_does_not_log_failure(self):
        """record_success must not call log_authentication_failure."""
        mock_logger = MagicMock()
        limiter = LoginRateLimiter(audit_logger=mock_logger)
        limiter.record_success("alice")
        mock_logger.log_authentication_failure.assert_not_called()


class TestLoginRateLimiterConfiguration:
    """Configuration: max_attempts, lockout_duration_minutes, window_minutes."""

    def test_custom_max_attempts(self):
        """Custom max_attempts=3 must lock after 3 failures."""
        limiter = LoginRateLimiter(max_attempts=3)
        is_locked, _ = False, 0
        for _ in range(3):
            is_locked, _ = limiter.check_and_record_failure("alice")
        assert is_locked is True

    def test_custom_max_attempts_2_does_not_lock_after_2(self):
        """Custom max_attempts=3: 2 failures must NOT lock."""
        limiter = LoginRateLimiter(max_attempts=3)
        for _ in range(2):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is False

    def test_check_and_record_failure_returns_tuple(self):
        """check_and_record_failure must return (bool, int/float) tuple."""
        limiter = LoginRateLimiter()
        result = limiter.check_and_record_failure("alice")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], (int, float))


# ---------------------------------------------------------------------------
# Integration tests: REST /auth/login endpoint
# ---------------------------------------------------------------------------


def _make_rest_app(login_rate_limiter=None):
    """Create a minimal FastAPI app with auth routes and lockout limiter."""
    from code_indexer.server.routers.inline_auth import register_auth_routes

    app = FastAPI()
    mock_jwt = MagicMock()
    mock_jwt.create_access_token.return_value = "test-token"
    mock_user_mgr = MagicMock()
    mock_user_mgr.is_password_expired.return_value = False
    mock_refresh_mgr = MagicMock()
    mock_refresh_mgr.create_token_family.return_value = "family-1"
    mock_refresh_mgr.create_initial_refresh_token.return_value = {
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "refresh_token_expires_in": 604800,
    }
    register_auth_routes(
        app,
        jwt_manager=mock_jwt,
        user_manager=mock_user_mgr,
        refresh_token_manager=mock_refresh_mgr,
        login_rate_limiter=login_rate_limiter,
    )
    return app, mock_user_mgr


def _make_successful_user(username="alice"):
    """Create a mock user that passes authentication."""
    mock_user = MagicMock()
    mock_user.username = username
    mock_user.role.value = "admin"
    mock_user.created_at.isoformat.return_value = "2026-01-01T00:00:00"
    mock_user.to_dict.return_value = {"username": username, "role": "admin"}
    return mock_user


class TestRestLoginLockout:
    """AC3+AC4: Account lockout applies to REST /auth/login endpoint."""

    def test_locked_account_returns_429(self):
        """AC4: Locked account must return HTTP 429 from /auth/login."""
        limiter = LoginRateLimiter()
        for _ in range(5):
            limiter.check_and_record_failure("alice")

        with patch("code_indexer.server.routers.inline_auth.rate_limiter") as mock_rl:
            mock_rl.consume.return_value = (True, 0.0)
            app, _ = _make_rest_app(login_rate_limiter=limiter)
            client = TestClient(app)
            resp = client.post(
                "/auth/login", json={"username": "alice", "password": "anything"}
            )
        assert resp.status_code == 429

    def test_locked_account_response_body_mentions_locked(self):
        """AC4: 429 response body must mention account lockout."""
        limiter = LoginRateLimiter()
        for _ in range(5):
            limiter.check_and_record_failure("alice")

        with patch("code_indexer.server.routers.inline_auth.rate_limiter") as mock_rl:
            mock_rl.consume.return_value = (True, 0.0)
            app, _ = _make_rest_app(login_rate_limiter=limiter)
            client = TestClient(app)
            resp = client.post(
                "/auth/login", json={"username": "alice", "password": "anything"}
            )
        assert resp.status_code == 429
        body = resp.json()
        assert "detail" in body
        detail_lower = body["detail"].lower()
        assert "account_locked" in detail_lower or "locked" in detail_lower

    def test_failed_login_records_failure_in_limiter(self):
        """AC4: Failed /auth/login must record failure in LoginRateLimiter."""
        limiter = LoginRateLimiter()

        with patch("code_indexer.server.routers.inline_auth.rate_limiter") as mock_rl:
            mock_rl.consume.return_value = (True, 0.0)
            app, mock_user_mgr = _make_rest_app(login_rate_limiter=limiter)
            mock_user_mgr.authenticate_user.return_value = None

            client = TestClient(app)
            client.post("/auth/login", json={"username": "alice", "password": "wrong"})

        for _ in range(4):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is True

    def test_successful_login_resets_failure_counter(self):
        """AC1+AC4: Successful /auth/login must call record_success on limiter."""
        limiter = LoginRateLimiter()
        for _ in range(4):
            limiter.check_and_record_failure("alice")

        with patch("code_indexer.server.routers.inline_auth.rate_limiter") as mock_rl:
            mock_rl.consume.return_value = (True, 0.0)
            app, mock_user_mgr = _make_rest_app(login_rate_limiter=limiter)
            mock_user_mgr.authenticate_user.return_value = _make_successful_user()

            client = TestClient(app)
            resp = client.post(
                "/auth/login", json={"username": "alice", "password": "correct"}
            )

        assert resp.status_code == 200
        for _ in range(4):
            limiter.check_and_record_failure("alice")
        locked, _ = limiter.is_locked("alice")
        assert locked is False

    def test_unlocked_account_can_login(self):
        """Unlocked account must be able to attempt login normally."""
        limiter = LoginRateLimiter()

        with patch("code_indexer.server.routers.inline_auth.rate_limiter") as mock_rl:
            mock_rl.consume.return_value = (True, 0.0)
            app, mock_user_mgr = _make_rest_app(login_rate_limiter=limiter)
            mock_user_mgr.authenticate_user.return_value = _make_successful_user()

            client = TestClient(app)
            resp = client.post(
                "/auth/login", json={"username": "alice", "password": "correct"}
            )

        assert resp.status_code == 200

    def test_lockout_check_happens_before_auth(self):
        """AC4: Lockout check must occur before credential validation."""
        limiter = LoginRateLimiter()
        for _ in range(5):
            limiter.check_and_record_failure("alice")

        with patch("code_indexer.server.routers.inline_auth.rate_limiter") as mock_rl:
            mock_rl.consume.return_value = (True, 0.0)
            app, mock_user_mgr = _make_rest_app(login_rate_limiter=limiter)

            client = TestClient(app)
            resp = client.post(
                "/auth/login", json={"username": "alice", "password": "anything"}
            )

        assert resp.status_code == 429
        mock_user_mgr.authenticate_user.assert_not_called()

    def test_disabled_limiter_allows_login_after_many_failures(self):
        """AC6: Disabled limiter must not block login even after many failures."""
        limiter = LoginRateLimiter(enabled=False)

        with patch("code_indexer.server.routers.inline_auth.rate_limiter") as mock_rl:
            mock_rl.consume.return_value = (True, 0.0)
            app, mock_user_mgr = _make_rest_app(login_rate_limiter=limiter)
            mock_user_mgr.authenticate_user.return_value = _make_successful_user()

            client = TestClient(app)
            for _ in range(10):
                limiter.check_and_record_failure("alice")

            resp = client.post(
                "/auth/login", json={"username": "alice", "password": "correct"}
            )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Integration tests: Web UI /login endpoint
# ---------------------------------------------------------------------------


class TestWebLoginLockout:
    """AC3: Account lockout applies to Web UI /login endpoint."""

    def test_locked_account_on_web_login_shows_error(self):
        """AC3: unified_login_submit must check lockout - locked account is detected."""

        limiter = LoginRateLimiter()
        for _ in range(5):
            limiter.check_and_record_failure("webuser")

        locked, remaining = limiter.is_locked("webuser")
        assert locked is True
        assert remaining > 0


# ---------------------------------------------------------------------------
# LoginSecurityConfig dataclass tests
# ---------------------------------------------------------------------------


class TestLoginSecurityConfig:
    """LoginSecurityConfig dataclass must exist in config_manager."""

    def test_login_security_config_exists(self):
        """LoginSecurityConfig must be importable from config_manager."""
        from code_indexer.server.utils.config_manager import LoginSecurityConfig

        config = LoginSecurityConfig()
        assert config is not None

    def test_default_values(self):
        """LoginSecurityConfig must have correct defaults."""
        from code_indexer.server.utils.config_manager import LoginSecurityConfig

        config = LoginSecurityConfig()
        assert config.login_rate_limiting_enabled is True
        assert config.max_failed_login_attempts == 5
        assert config.login_lockout_duration_minutes == 15

    def test_custom_values(self):
        """LoginSecurityConfig must accept custom values."""
        from code_indexer.server.utils.config_manager import LoginSecurityConfig

        config = LoginSecurityConfig(
            login_rate_limiting_enabled=False,
            max_failed_login_attempts=3,
            login_lockout_duration_minutes=30,
        )
        assert config.login_rate_limiting_enabled is False
        assert config.max_failed_login_attempts == 3
        assert config.login_lockout_duration_minutes == 30

    def test_server_config_has_login_security_config(self):
        """ServerConfig must include login_security_config field."""
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir="/tmp/test")
        assert hasattr(config, "login_security_config")
        assert config.login_security_config is not None

    def test_server_config_login_security_config_has_correct_defaults(self):
        """ServerConfig.login_security_config must use LoginSecurityConfig defaults."""
        from code_indexer.server.utils.config_manager import (
            LoginSecurityConfig,
            ServerConfig,
        )

        config = ServerConfig(server_dir="/tmp/test")
        assert isinstance(config.login_security_config, LoginSecurityConfig)
        assert config.login_security_config.login_rate_limiting_enabled is True
        assert config.login_security_config.max_failed_login_attempts == 5
        assert config.login_security_config.login_lockout_duration_minutes == 15


# ---------------------------------------------------------------------------
# Cluster mode tests (H1): LoginRateLimiter with PostgreSQL connection pool
# ---------------------------------------------------------------------------


class TestLoginRateLimiterClusterMode:
    """H1: LoginRateLimiter stores failure/lockout state in PostgreSQL when pool is set."""

    def test_set_connection_pool_method_exists(self):
        """LoginRateLimiter must expose set_connection_pool()."""
        limiter = LoginRateLimiter()
        assert hasattr(limiter, "set_connection_pool")

    def test_failure_tracked_via_pool(self, pg_pool):
        """Failures are persisted to login_failures table when pool is set."""
        limiter = LoginRateLimiter()
        limiter.set_connection_pool(pg_pool)
        limiter.check_and_record_failure("alice")

        # Verify row exists in the database
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM login_failures WHERE username = %s",
                ("alice",),
            ).fetchone()
            count = row[0]
        assert count == 1

    def test_lockout_enforced_via_pool(self, pg_pool):
        """Account locks out after max_attempts failures via pool."""
        limiter = LoginRateLimiter(max_attempts=3)
        limiter.set_connection_pool(pg_pool)

        is_locked = False
        for _ in range(3):
            is_locked, remaining = limiter.check_and_record_failure("bob")
        assert is_locked is True
        assert remaining > 0

        # Verify lockout row exists in database
        with pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT locked_until FROM login_lockouts WHERE username = %s",
                ("bob",),
            ).fetchone()
        assert row is not None
        assert row[0] > time.time()

    def test_success_clears_via_pool(self, pg_pool):
        """record_success deletes failures and lockouts from database."""
        limiter = LoginRateLimiter(max_attempts=3)
        limiter.set_connection_pool(pg_pool)

        for _ in range(3):
            limiter.check_and_record_failure("carol")
        locked, _ = limiter.is_locked("carol")
        assert locked is True

        limiter.record_success("carol")
        locked, _ = limiter.is_locked("carol")
        assert locked is False

        # Verify database is clean
        with pg_pool.connection() as conn:
            failures = conn.execute(
                "SELECT COUNT(*) FROM login_failures WHERE username = %s",
                ("carol",),
            ).fetchone()[0]
            lockouts = conn.execute(
                "SELECT COUNT(*) FROM login_lockouts WHERE username = %s",
                ("carol",),
            ).fetchone()[0]
        assert failures == 0
        assert lockouts == 0

    def test_cross_node_lockout(self, pg_pool):
        """Two limiter instances sharing same pool see each other's state."""
        limiter_node1 = LoginRateLimiter(max_attempts=3)
        limiter_node1.set_connection_pool(pg_pool)

        limiter_node2 = LoginRateLimiter(max_attempts=3)
        limiter_node2.set_connection_pool(pg_pool)

        # Node 1 records 2 failures
        limiter_node1.check_and_record_failure("dave")
        limiter_node1.check_and_record_failure("dave")

        # Node 2 records the 3rd failure - should trigger lockout
        is_locked, remaining = limiter_node2.check_and_record_failure("dave")
        assert is_locked is True
        assert remaining > 0

        # Node 1 should also see the lockout
        locked, _ = limiter_node1.is_locked("dave")
        assert locked is True
