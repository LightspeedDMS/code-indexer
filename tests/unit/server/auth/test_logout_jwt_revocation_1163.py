"""Tests for JWT Logout Token Revocation + Blacklist Pruning (Story #1163).

Acceptance criteria:
  AC1 - Both logout routes (web + user-portal) blacklist the JWT jti, non-fatal.
  AC2 - TokenBlacklist.prune_expired() deletes expired rows (real SQLite);
        DataRetentionScheduler._execute_cleanup_sqlite calls prune_expired()
        with ttl_seconds = jwt_expiration_minutes * 60.
  AC3 - Single-worker logout still blacklists + redirects correctly (no regression).

Uses real SQLite only; PostgreSQL prune (`_pg_prune`) is not unit-tested here; it mirrors
the proven SQLite prune semantics and the existing `_pg_add`/`_pg_contains` style, and is
validated against real PostgreSQL at the epic-level staging gate.
"""

from __future__ import annotations

import sqlite3
import tempfile
import os
import time
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_JWT_SECRET = "test-secret-key-for-story-1163-unit-tests"


# ---------------------------------------------------------------------------
# SQLite helpers (real DB, no mocking)
# ---------------------------------------------------------------------------


def _make_blacklist_db() -> str:
    """Create a temporary SQLite DB with the token_blacklist table."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    with sqlite3.connect(tmp.name) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS token_blacklist "
            "(jti TEXT PRIMARY KEY, blacklisted_at REAL NOT NULL)"
        )
        conn.commit()
    return tmp.name


def _insert_row(db_path: str, jti: str, blacklisted_at: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO token_blacklist (jti, blacklisted_at) VALUES (?, ?)",
            (jti, blacklisted_at),
        )
        conn.commit()


def _row_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM token_blacklist").fetchone()[0])


def _surviving_jti(db_path: str) -> str:
    with sqlite3.connect(db_path) as conn:
        return str(conn.execute("SELECT jti FROM token_blacklist").fetchone()[0])


# ---------------------------------------------------------------------------
# JWT / scheduler helpers
# ---------------------------------------------------------------------------


def _make_jwt_manager():
    """Create a real JWTManager for testing."""
    from code_indexer.server.auth.jwt_manager import JWTManager

    return JWTManager(
        secret_key=TEST_JWT_SECRET,
        token_expiration_minutes=10,
    )


def _make_config_service(jwt_expiration_minutes: int = 10) -> Any:
    """Build a minimal config_service stub used by DataRetentionScheduler."""
    ret_cfg = MagicMock()
    ret_cfg.operational_logs_retention_hours = 168
    ret_cfg.audit_logs_retention_hours = 720
    ret_cfg.sync_jobs_retention_hours = 168
    ret_cfg.dep_map_history_retention_hours = 720
    ret_cfg.background_jobs_retention_hours = 24
    ret_cfg.cleanup_interval_hours = 1

    config = MagicMock()
    config.data_retention_config = ret_cfg
    config.jwt_expiration_minutes = jwt_expiration_minutes

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


def _make_scheduler(config_service: Any, tmp_path: Path, **kwargs: Any) -> Any:
    from code_indexer.server.services.data_retention_scheduler import (
        DataRetentionScheduler,
    )

    return DataRetentionScheduler(
        log_db_path=tmp_path / "logs.db",
        main_db_path=tmp_path / "main.db",
        groups_db_path=tmp_path / "groups.db",
        config_service=config_service,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Logout route test helper
# ---------------------------------------------------------------------------


def _run_logout_route_test(
    router: Any,
    prefix: str,
    route_path: str,
    token: str,
    jwt_mgr: Any,
) -> tuple:
    """
    Drive a logout route via TestClient.

    Returns (response, was_blacklisted) where was_blacklisted is a bool
    captured BEFORE state is restored in the finally block.
    """
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from code_indexer.server.app import get_token_blacklist
    from code_indexer.server.auth import dependencies as deps

    app = FastAPI()
    app.include_router(router, prefix=prefix)

    original_jm = deps.jwt_manager
    deps.jwt_manager = jwt_mgr

    bl = get_token_blacklist()
    orig_sqlite = bl._sqlite_db_path
    orig_pool = bl._pool
    bl._local.clear()
    db_path = _make_blacklist_db()
    bl.set_sqlite_path(db_path)

    was_blacklisted: bool = False
    response = None
    try:
        payload = jwt_mgr.validate_token(token)
        jti = payload["jti"]

        with TestClient(app, follow_redirects=False) as client:
            response = client.get(
                f"{prefix}{route_path}",
                cookies={"cidx_session": token},
            )

        # Capture result BEFORE finally restores state
        was_blacklisted = bl.contains(jti)
    finally:
        deps.jwt_manager = original_jm
        bl._sqlite_db_path = orig_sqlite
        bl._pool = orig_pool
        bl._local.clear()
        try:
            os.unlink(db_path)
        except OSError:
            pass

    return response, was_blacklisted


# ---------------------------------------------------------------------------
# AC2: TokenBlacklist.prune_expired() — SQLite pruning cutoff
# ---------------------------------------------------------------------------


class TestTokenBlacklistPruneExpiredSqlite:
    """Real SQLite tests for TokenBlacklist.prune_expired()."""

    def test_prune_expired_deletes_old_row_keeps_fresh_row(self):
        """Expired row is deleted; fresh row is retained; return count == 1."""
        from code_indexer.server.app import TokenBlacklist

        db_path = _make_blacklist_db()
        try:
            bl = TokenBlacklist()
            bl.set_sqlite_path(db_path)

            now = time.time()
            _insert_row(
                db_path, "jti-expired", now - 700
            )  # 700s old, TTL=600s -> expired
            _insert_row(db_path, "jti-fresh", now - 100)  # 100s old -> fresh

            deleted = bl.prune_expired(ttl_seconds=600)

            assert deleted == 1, f"Expected 1 row deleted, got {deleted}"
            assert _row_count(db_path) == 1
            assert _surviving_jti(db_path) == "jti-fresh"
        finally:
            os.unlink(db_path)

    def test_prune_expired_returns_zero_when_nothing_to_delete(self):
        """When no rows are expired, prune_expired returns 0."""
        from code_indexer.server.app import TokenBlacklist

        db_path = _make_blacklist_db()
        try:
            bl = TokenBlacklist()
            bl.set_sqlite_path(db_path)

            now = time.time()
            _insert_row(db_path, "jti-a", now - 10)
            _insert_row(db_path, "jti-b", now - 20)

            assert bl.prune_expired(ttl_seconds=600) == 0
            assert _row_count(db_path) == 2
        finally:
            os.unlink(db_path)

    def test_prune_expired_deletes_multiple_expired_rows(self):
        """All expired rows are deleted; fresh row survives."""
        from code_indexer.server.app import TokenBlacklist

        db_path = _make_blacklist_db()
        try:
            bl = TokenBlacklist()
            bl.set_sqlite_path(db_path)

            now = time.time()
            _insert_row(db_path, "jti-old-1", now - 1000)
            _insert_row(db_path, "jti-old-2", now - 800)
            _insert_row(db_path, "jti-old-3", now - 700)
            _insert_row(db_path, "jti-new", now - 10)

            assert bl.prune_expired(ttl_seconds=600) == 3
            assert _row_count(db_path) == 1
        finally:
            os.unlink(db_path)

    def test_prune_expired_no_backend_returns_zero(self):
        """prune_expired on a no-backend instance returns 0 (no-op, no raise)."""
        from code_indexer.server.app import TokenBlacklist

        bl = TokenBlacklist()
        assert bl.prune_expired(ttl_seconds=600) == 0

    def test_prune_expired_evicts_from_local_set(self):
        """After pruning, the evicted jti is no longer in the local in-memory set."""
        from code_indexer.server.app import TokenBlacklist

        db_path = _make_blacklist_db()
        try:
            bl = TokenBlacklist()
            bl.set_sqlite_path(db_path)

            now = time.time()
            jti = "jti-to-evict"
            bl._local.add(jti)
            _insert_row(db_path, jti, now - 700)

            bl.prune_expired(ttl_seconds=600)

            assert jti not in bl._local, "Pruned jti must be removed from local set"
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# AC2: DataRetentionScheduler (SQLite path) calls prune_expired with right TTL
# ---------------------------------------------------------------------------


class TestDataRetentionSchedulerBlacklistPruning:
    """DataRetentionScheduler must invoke prune_expired with jwt_expiration_minutes*60."""

    def test_sqlite_cleanup_result_contains_token_blacklist_deleted(
        self, tmp_path: Path
    ) -> None:
        """_execute_cleanup_sqlite() must include 'token_blacklist_deleted' in result."""
        scheduler = _make_scheduler(
            _make_config_service(jwt_expiration_minutes=10), tmp_path
        )

        result = scheduler._execute_cleanup_sqlite()

        assert "token_blacklist_deleted" in result, (
            "Missing 'token_blacklist_deleted' key in sqlite cleanup result"
        )
        assert isinstance(result["token_blacklist_deleted"], int)

    def test_sqlite_cleanup_total_includes_token_blacklist(
        self, tmp_path: Path
    ) -> None:
        """total_deleted must be the sum of all per-table counts including token_blacklist."""
        scheduler = _make_scheduler(
            _make_config_service(jwt_expiration_minutes=10), tmp_path
        )

        result = scheduler._execute_cleanup_sqlite()

        expected_total = (
            result.get("logs_deleted", 0)
            + result.get("audit_logs_deleted", 0)
            + result.get("sync_jobs_deleted", 0)
            + result.get("dep_map_history_deleted", 0)
            + result.get("background_jobs_deleted", 0)
            + result.get("token_blacklist_deleted", 0)
        )
        assert result["total_deleted"] == expected_total

    def test_sqlite_cleanup_calls_prune_expired_with_correct_ttl(
        self, tmp_path: Path
    ) -> None:
        """Scheduler calls prune_expired(ttl_seconds=jwt_expiration_minutes*60)."""
        jwt_minutes = 7  # non-default to detect hardcoding
        expected_ttl = jwt_minutes * 60

        scheduler = _make_scheduler(
            _make_config_service(jwt_expiration_minutes=jwt_minutes), tmp_path
        )

        from code_indexer.server.app import get_token_blacklist

        bl = get_token_blacklist()
        with patch.object(bl, "prune_expired", wraps=bl.prune_expired) as spy:
            scheduler._execute_cleanup_sqlite()

        spy.assert_called_once_with(ttl_seconds=expected_ttl)


# ---------------------------------------------------------------------------
# AC1: _extract_jti_from_request helper
# ---------------------------------------------------------------------------


class TestExtractJtiFromRequest:
    """Unit tests for the _extract_jti_from_request module-private helper.

    Note: deps.jwt_manager is None at module load time in unit tests.
    Tests that need successful JWT decoding must patch deps.jwt_manager
    with a real JWTManager instance.
    """

    def test_reads_jti_from_cookie(self):
        """Extracts jti from cidx_session cookie when no Authorization header."""
        from code_indexer.server.web.routes import _extract_jti_from_request
        from code_indexer.server.auth import dependencies as deps

        jwt_mgr = _make_jwt_manager()
        token = jwt_mgr.create_token({"username": "alice", "role": "admin"})
        expected_jti = jwt_mgr.validate_token(token)["jti"]

        request = MagicMock()
        request.headers.get.return_value = None
        request.cookies.get.return_value = token

        original_jm = deps.jwt_manager
        deps.jwt_manager = jwt_mgr
        try:
            assert _extract_jti_from_request(request) == expected_jti
        finally:
            deps.jwt_manager = original_jm

    def test_reads_jti_from_authorization_header_first(self):
        """Authorization header takes priority over cookie."""
        from code_indexer.server.web.routes import _extract_jti_from_request
        from code_indexer.server.auth import dependencies as deps

        jwt_mgr = _make_jwt_manager()
        token = jwt_mgr.create_token({"username": "bob", "role": "user"})
        expected_jti = jwt_mgr.validate_token(token)["jti"]

        request = MagicMock()
        request.headers.get.return_value = f"Bearer {token}"
        request.cookies.get.return_value = None

        original_jm = deps.jwt_manager
        deps.jwt_manager = jwt_mgr
        try:
            assert _extract_jti_from_request(request) == expected_jti
        finally:
            deps.jwt_manager = original_jm

    def test_returns_none_when_no_token(self):
        """Returns None when neither header nor cookie is present."""
        from code_indexer.server.web.routes import _extract_jti_from_request

        request = MagicMock()
        request.headers.get.return_value = None
        request.cookies.get.return_value = None

        # deps.jwt_manager is None here — early exit path
        assert _extract_jti_from_request(request) is None

    def test_returns_none_for_malformed_token(self):
        """Returns None (does not raise) for a malformed JWT."""
        from code_indexer.server.web.routes import _extract_jti_from_request
        from code_indexer.server.auth import dependencies as deps

        jwt_mgr = _make_jwt_manager()
        request = MagicMock()
        request.headers.get.return_value = "Bearer not.a.real.jwt"
        request.cookies.get.return_value = None

        original_jm = deps.jwt_manager
        deps.jwt_manager = jwt_mgr
        try:
            assert _extract_jti_from_request(request) is None
        finally:
            deps.jwt_manager = original_jm


# ---------------------------------------------------------------------------
# AC1: Non-fatal logout behavior
# ---------------------------------------------------------------------------


class TestLogoutNonFatal:
    """AC1 (non-fatal): Logout returns 303 even when JWT extraction fails."""

    def test_web_logout_no_token_redirects_303(self):
        """Web logout with no JWT still returns 303 → /login."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from code_indexer.server.web.routes import web_router

        app = FastAPI()
        app.include_router(web_router)

        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/logout")

        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    def test_web_logout_malformed_token_redirects_303_and_logs_warning(
        self, caplog: Any
    ):
        """Web logout with malformed JWT logs WARNING and still returns 303."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from code_indexer.server.web.routes import web_router

        app = FastAPI()
        app.include_router(web_router)

        with caplog.at_level(logging.WARNING):
            with TestClient(app, follow_redirects=False) as client:
                response = client.get(
                    "/logout",
                    cookies={"cidx_session": "bad.token.value"},
                )

        assert response.status_code == 303
        warning_texts = " ".join(
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert any(
            kw in warning_texts.lower()
            for kw in ("logout", "jti", "revoc", "blacklist", "token")
        ), f"Expected a WARNING about jti/token/revocation, got: {warning_texts!r}"


# ---------------------------------------------------------------------------
# AC1 + AC3: Logout routes blacklist the JWT jti
# ---------------------------------------------------------------------------


class TestLogoutBlacklistsJti:
    """AC1 + AC3: Both logout routes add the JWT jti to the TokenBlacklist."""

    def test_web_logout_blacklists_jti_from_cookie(self):
        """Web /logout blacklists the JWT jti when token is in cidx_session cookie."""
        from code_indexer.server.web.routes import web_router

        jwt_mgr = _make_jwt_manager()
        token = jwt_mgr.create_token({"username": "alice", "role": "admin"})

        response, was_blacklisted = _run_logout_route_test(
            web_router, prefix="", route_path="/logout", token=token, jwt_mgr=jwt_mgr
        )

        assert response.status_code == 303
        assert was_blacklisted, "jti must be blacklisted after web logout"

    def test_user_logout_blacklists_jti_from_cookie(self):
        """User-portal /user/logout blacklists the JWT jti."""
        from code_indexer.server.web.routes import user_router

        jwt_mgr = _make_jwt_manager()
        token = jwt_mgr.create_token({"username": "bob", "role": "user"})

        response, was_blacklisted = _run_logout_route_test(
            user_router,
            prefix="/user",
            route_path="/logout",
            token=token,
            jwt_mgr=jwt_mgr,
        )

        assert response.status_code == 303
        assert was_blacklisted, "jti must be blacklisted after user portal logout"

    def test_single_worker_logout_blacklists_and_redirects_to_login(self):
        """AC3: Single-worker path: logout blacklists jti and redirects to /login."""
        from code_indexer.server.web.routes import web_router

        jwt_mgr = _make_jwt_manager()
        token = jwt_mgr.create_token({"username": "carol", "role": "admin"})

        response, was_blacklisted = _run_logout_route_test(
            web_router, prefix="", route_path="/logout", token=token, jwt_mgr=jwt_mgr
        )

        assert response.status_code == 303
        assert "/login" in response.headers["location"]
        assert was_blacklisted, "jti must be blacklisted after single-worker logout"
