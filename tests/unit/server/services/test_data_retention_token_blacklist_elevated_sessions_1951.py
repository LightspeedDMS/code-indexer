"""
Regression tests for issue #1951's acceptance criterion:

    "Regression test drives the cleanup against seeded rows in both
     backends."

Seeds a row OLDER than the configured retention window directly into
`token_blacklist` and `elevated_sessions`, runs the real
DataRetentionScheduler safe-wrapper (the same integration path production
uses) against a REAL backend -- SQLite here; PostgreSQL gated on
TEST_POSTGRES_DSN below -- and asserts the row is gone and the table is not
recorded in failed_tables.

Investigation for #1951 found both backends already prune these two
tables correctly today (see test_data_retention_underlying_error_text_1951.py
for the full root-cause writeup and evidence trail); these tests are the
regression guard the issue explicitly requires so a future change cannot
silently reintroduce the original SQLite lock-contention style failure
(Bug #1758) or a type mismatch between the two backends without a fast
test catching it.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

HAS_PSYCOPG = importlib.util.find_spec("psycopg") is not None

_logger = logging.getLogger(__name__)

# Retention-config values used to build the fake config_service. These mirror
# the defaults already used by test_data_retention_per_table_isolation.py and
# test_sqlite_lock_timeout_1758.py -- kept as named constants here so the
# significance of each value (a duration in hours/minutes, not an arbitrary
# number) is explicit at the call site.
_OPERATIONAL_LOGS_RETENTION_HOURS = 168  # 7 days
_AUDIT_LOGS_RETENTION_HOURS = 720  # 30 days
_SYNC_JOBS_RETENTION_HOURS = 168  # 7 days
_DEP_MAP_HISTORY_RETENTION_HOURS = 720  # 30 days
_BACKGROUND_JOBS_RETENTION_HOURS = 24  # 1 day
_CLEANUP_INTERVAL_HOURS = 1
_JWT_EXPIRATION_MINUTES = 10

# How far in the past to seed the "expired" row's timestamp. Must exceed
# every TTL derived from the constants above (the longest is
# _JWT_EXPIRATION_MINUTES * 60 = 600s, and ElevatedSessionManager's default
# max_age_seconds is 1800s) by a comfortable margin.
_EXPIRED_TIMESTAMP_OFFSET_SECONDS = 100_000

# psycopg3 ConnectionPool sizing for the real-PG fixture -- small and fixed
# since these tests only ever hold one connection at a time.
_PG_POOL_MIN_SIZE = 1
_PG_POOL_MAX_SIZE = 4


def _make_config() -> Any:
    """Build a minimal config_service test double.

    Returns `Any` (a `unittest.mock.MagicMock`) rather than a typed
    ConfigService/ServerConfig, because this is a stand-in test double with
    no fixed production protocol to type against -- only the specific
    attributes DataRetentionScheduler reads are populated. Mirrors the
    identical helper already used by test_data_retention_per_table_isolation.py
    and test_sqlite_lock_timeout_1758.py.
    """
    ret_cfg = MagicMock()
    ret_cfg.operational_logs_retention_hours = _OPERATIONAL_LOGS_RETENTION_HOURS
    ret_cfg.audit_logs_retention_hours = _AUDIT_LOGS_RETENTION_HOURS
    ret_cfg.sync_jobs_retention_hours = _SYNC_JOBS_RETENTION_HOURS
    ret_cfg.dep_map_history_retention_hours = _DEP_MAP_HISTORY_RETENTION_HOURS
    ret_cfg.background_jobs_retention_hours = _BACKGROUND_JOBS_RETENTION_HOURS
    ret_cfg.cleanup_interval_hours = _CLEANUP_INTERVAL_HOURS

    config = MagicMock()
    config.data_retention_config = ret_cfg
    config.jwt_expiration_minutes = _JWT_EXPIRATION_MINUTES

    config_service = MagicMock()
    config_service.get_config.return_value = config
    return config_service


def _make_scheduler(config_service: Any, tmp_path: Path) -> Any:
    """Construct a real DataRetentionScheduler for the test.

    `config_service` is typed `Any` because it is the `_make_config()`
    MagicMock double above, not the production ConfigService class; the
    return value is the real `DataRetentionScheduler` (imported locally to
    avoid a module-level import before PYTHONPATH is set up by the test
    runner), typed `Any` here only to avoid re-declaring that import at
    module scope.
    """
    from code_indexer.server.services.data_retention_scheduler import (
        DataRetentionScheduler,
    )

    return DataRetentionScheduler(
        log_db_path=tmp_path / "logs.db",
        main_db_path=tmp_path / "main.db",
        groups_db_path=tmp_path / "groups.db",
        config_service=config_service,
        storage_mode="sqlite",
    )


def _expired_timestamp() -> float:
    """A unix timestamp comfortably older than every retention TTL used here."""
    return time.time() - _EXPIRED_TIMESTAMP_OFFSET_SECONDS


# ---------------------------------------------------------------------------
# SQLite backend (solo) -- real TokenBlacklist / ElevatedSessionManager
# ---------------------------------------------------------------------------


class TestSqliteSeededRowsArePruned:
    def test_expired_token_blacklist_row_is_pruned(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.app import TokenBlacklist

        db_path = tmp_path / "cidx_server.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        blacklist = TokenBlacklist()
        blacklist.set_sqlite_path(str(db_path))

        expired_jti = f"expired-{uuid.uuid4().hex}"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO token_blacklist (jti, blacklisted_at) VALUES (?, ?)",
                (expired_jti, _expired_timestamp()),
            )
            conn.commit()
        finally:
            conn.close()

        scheduler = _make_scheduler(_make_config(), tmp_path)
        failed_tables: List[str] = []
        with patch(
            "code_indexer.server.app.get_token_blacklist", return_value=blacklist
        ):
            deleted = scheduler._safe_prune_token_blacklist(
                jwt_expiration_minutes=_JWT_EXPIRATION_MINUTES,
                failed_tables=failed_tables,
            )

        assert deleted == 1
        assert failed_tables == []

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT 1 FROM token_blacklist WHERE jti = ?", (expired_jti,)
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "Expired token_blacklist row must be pruned"

    def test_expired_elevated_sessions_row_is_pruned(self, tmp_path: Path) -> None:
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        db_path = tmp_path / "cidx_server.db"
        manager = ElevatedSessionManager(db_path=str(db_path))

        expired_key = f"expired-session-{uuid.uuid4().hex}"
        expired_ts = _expired_timestamp()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO elevated_sessions "
                "(session_key, username, elevated_at, last_touched_at, "
                " elevated_from_ip, scope) VALUES (?, ?, ?, ?, ?, ?)",
                (expired_key, "someuser", expired_ts, expired_ts, "127.0.0.1", "full"),
            )
            conn.commit()
        finally:
            conn.close()

        scheduler = _make_scheduler(_make_config(), tmp_path)
        failed_tables: List[str] = []
        with patch(
            "code_indexer.server.auth.elevated_session_manager."
            "elevated_session_manager",
            manager,
        ):
            deleted = scheduler._safe_prune_elevated_sessions(
                failed_tables=failed_tables
            )

        assert deleted == 1
        assert failed_tables == []

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT 1 FROM elevated_sessions WHERE session_key = ?",
                (expired_key,),
            ).fetchone()
        finally:
            conn.close()
        assert row is None, "Expired elevated_sessions row must be pruned"


# ---------------------------------------------------------------------------
# PostgreSQL backend (cluster) -- gated on TEST_POSTGRES_DSN
# ---------------------------------------------------------------------------

_REAL_PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

_REAL_PG_SKIP = pytest.mark.skipif(
    not HAS_PSYCOPG or not _REAL_PG_DSN,
    reason="psycopg unavailable or TEST_POSTGRES_DSN not set — real-PostgreSQL "
    "test skipped",
)


@pytest.fixture()
def real_pg_pool():
    """Real psycopg3 ConnectionPool against TEST_POSTGRES_DSN.

    Skipped automatically when TEST_POSTGRES_DSN is unset or psycopg is not
    installed (the skipif marker fires before the fixture body runs), mirroring
    tests/unit/server/auth/oidc/test_oidc_state_shared_store_1224.py.
    """
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pool = ConnectionPool(
        _REAL_PG_DSN,
        min_size=_PG_POOL_MIN_SIZE,
        max_size=_PG_POOL_MAX_SIZE,
        name="retention-1951-test",
    )
    try:
        with pool.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS token_blacklist ("
                "jti TEXT PRIMARY KEY, blacklisted_at DOUBLE PRECISION NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS elevated_sessions ("
                "session_key TEXT PRIMARY KEY, username TEXT NOT NULL, "
                "elevated_at DOUBLE PRECISION NOT NULL, "
                "last_touched_at DOUBLE PRECISION NOT NULL, "
                "elevated_from_ip TEXT, scope TEXT NOT NULL DEFAULT 'full')"
            )
            conn.commit()
        yield pool
    finally:
        try:
            pool._pool.close()  # type: ignore[attr-defined]
        except Exception:
            _logger.warning("retention-1951-test pool teardown failed", exc_info=True)


@_REAL_PG_SKIP
class TestRealPgSeededRowsArePruned:
    """Drives the real DataRetentionScheduler safe wrappers -- the same
    integration path production uses -- against a REAL PostgreSQL database,
    mirroring the SQLite tests above exactly. Skips automatically when
    TEST_POSTGRES_DSN is unset so it never blocks fast-automation.sh."""

    def test_expired_token_blacklist_row_is_pruned_pg(
        self, real_pg_pool, tmp_path: Path
    ) -> None:
        from code_indexer.server.app import TokenBlacklist

        blacklist = TokenBlacklist()
        blacklist.set_connection_pool(real_pg_pool)

        expired_jti = f"pg-expired-{uuid.uuid4().hex}"
        with real_pg_pool.connection() as conn:
            conn.execute(
                "INSERT INTO token_blacklist (jti, blacklisted_at) VALUES (%s, %s)",
                (expired_jti, _expired_timestamp()),
            )
            conn.commit()

        scheduler = _make_scheduler(_make_config(), tmp_path)
        failed_tables: List[str] = []
        with patch(
            "code_indexer.server.app.get_token_blacklist", return_value=blacklist
        ):
            deleted = scheduler._safe_prune_token_blacklist(
                jwt_expiration_minutes=_JWT_EXPIRATION_MINUTES,
                failed_tables=failed_tables,
            )

        assert deleted >= 1
        assert failed_tables == []

        with real_pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM token_blacklist WHERE jti = %s", (expired_jti,)
            ).fetchone()
        assert row is None, "Expired token_blacklist row must be pruned (PG)"

    def test_expired_elevated_sessions_row_is_pruned_pg(
        self, real_pg_pool, tmp_path: Path
    ) -> None:
        from code_indexer.server.auth.elevated_session_manager import (
            ElevatedSessionManager,
        )

        manager = ElevatedSessionManager()
        manager.set_connection_pool(real_pg_pool)

        expired_key = f"pg-expired-session-{uuid.uuid4().hex}"
        expired_ts = _expired_timestamp()
        with real_pg_pool.connection() as conn:
            conn.execute(
                "INSERT INTO elevated_sessions "
                "(session_key, username, elevated_at, last_touched_at, "
                " elevated_from_ip, scope) VALUES (%s, %s, %s, %s, %s, %s)",
                (expired_key, "someuser", expired_ts, expired_ts, "127.0.0.1", "full"),
            )
            conn.commit()

        scheduler = _make_scheduler(_make_config(), tmp_path)
        failed_tables: List[str] = []
        with patch(
            "code_indexer.server.auth.elevated_session_manager."
            "elevated_session_manager",
            manager,
        ):
            deleted = scheduler._safe_prune_elevated_sessions(
                failed_tables=failed_tables
            )

        assert deleted >= 1
        assert failed_tables == []

        with real_pg_pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM elevated_sessions WHERE session_key = %s",
                (expired_key,),
            ).fetchone()
        assert row is None, "Expired elevated_sessions row must be pruned (PG)"
