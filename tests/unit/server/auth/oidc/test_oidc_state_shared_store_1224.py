"""Tests for Bug #1224: OIDC state must be stored in shared SQLite/PG store.

Cross-worker/process state sharing is the core regression: state created on
worker A must be validated on worker B backed by the same store.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.oidc.state_manager import StateManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    """Return a path to a fresh SQLite DB for each test."""
    return str(tmp_path / "oidc_test.db")


def make_manager(db_path: str) -> StateManager:
    """Create a StateManager pointed at the given SQLite file."""
    manager = StateManager()
    manager.set_sqlite_path(db_path)
    return manager


# ---------------------------------------------------------------------------
# Core regression: cross-worker/process sharing
# ---------------------------------------------------------------------------


class TestCrossWorkerSQLiteSharing:
    """Two StateManager instances on the same SQLite file share state
    transparently — this models separate uvicorn workers on the same node."""

    def test_state_created_on_instance_a_validated_on_instance_b(self, db_path):
        """State written by instance A is readable by instance B."""
        manager_a = make_manager(db_path)
        manager_b = make_manager(db_path)

        state_data = {"code_verifier": "pkce-verifier-abc", "redirect_to": "/admin"}
        token = manager_a.create_state(state_data)

        result = manager_b.validate_state(token)
        assert result == state_data

    def test_validate_state_is_single_use(self, db_path):
        """Second validate of the same token returns None (delete-on-read)."""
        manager = make_manager(db_path)

        token = manager.create_state({"code_verifier": "verifier-x"})
        assert manager.validate_state(token) is not None
        assert manager.validate_state(token) is None

    def test_single_use_atomic_across_instances(self, db_path):
        """Delete-on-validate is visible to a second instance immediately."""
        manager_a = make_manager(db_path)
        manager_b = make_manager(db_path)

        token = manager_a.create_state({"code_verifier": "v1"})
        manager_a.validate_state(token)  # A consumes it
        assert manager_b.validate_state(token) is None  # B must see it gone

    def test_expired_state_returns_none(self, db_path):
        """TTL-expired state returns None from validate_state."""
        manager = make_manager(db_path)
        token = manager.create_state({"code_verifier": "expire-test"})

        # Backdate expires_at so TTL is exceeded
        past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE oidc_state_tokens SET expires_at = ? WHERE state_token = ?",
                (past, token),
            )

        assert manager.validate_state(token) is None

    def test_unknown_token_returns_none(self, db_path):
        """validate_state on unknown token returns None without raising."""
        manager = make_manager(db_path)
        assert manager.validate_state("totally-unknown-token-xyz") is None

    def test_code_verifier_and_redirect_to_round_trip(self, db_path):
        """code_verifier and redirect_to are preserved exactly across create/validate."""
        manager = make_manager(db_path)
        state_data = {
            "code_verifier": "secret_verifier_abc123",
            "redirect_to": "/some/internal/path",
            "extra_field": "preserved",
        }
        token = manager.create_state(state_data)
        result = manager.validate_state(token)

        assert result["code_verifier"] == "secret_verifier_abc123"
        assert result["redirect_to"] == "/some/internal/path"
        assert result["extra_field"] == "preserved"

    def test_redirect_to_is_optional(self, db_path):
        """State with only code_verifier round-trips without redirect_to."""
        manager = make_manager(db_path)
        state_data = {"code_verifier": "minimal-verifier"}
        token = manager.create_state(state_data)
        result = manager.validate_state(token)

        assert result == state_data
        assert "redirect_to" not in result


# ---------------------------------------------------------------------------
# prune_expired
# ---------------------------------------------------------------------------


class TestPruneExpired:
    """prune_expired removes only expired rows and returns an accurate count."""

    def test_prune_expired_removes_expired_rows_leaves_valid(self, db_path):
        """prune_expired deletes rows past TTL, leaves valid rows intact."""
        manager = make_manager(db_path)

        valid_token = manager.create_state({"code_verifier": "valid"})
        expired_token = manager.create_state({"code_verifier": "expired"})

        # Backdate the expired token
        past = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE oidc_state_tokens SET expires_at = ? WHERE state_token = ?",
                (past, expired_token),
            )

        deleted = manager.prune_expired(ttl_seconds=300)
        assert deleted == 1

        assert manager.validate_state(valid_token) is not None
        assert manager.validate_state(expired_token) is None

    def test_prune_expired_returns_zero_when_nothing_to_prune(self, db_path):
        """prune_expired returns 0 when no expired rows exist."""
        manager = make_manager(db_path)
        manager.create_state({"code_verifier": "fresh"})
        assert manager.prune_expired(ttl_seconds=300) == 0

    def test_prune_expired_empty_table_returns_zero(self, db_path):
        """prune_expired on empty table returns 0 without error."""
        manager = make_manager(db_path)
        assert manager.prune_expired(ttl_seconds=300) == 0


# ---------------------------------------------------------------------------
# set_sqlite_path wiring behavior
# ---------------------------------------------------------------------------


class TestSetSQLitePathBehavior:
    """set_sqlite_path wiring and schema creation."""

    def test_set_sqlite_path_creates_schema(self, db_path):
        """set_sqlite_path creates oidc_state_tokens table in the target DB."""
        make_manager(db_path)  # side-effect: creates schema via set_sqlite_path

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='oidc_state_tokens'"
            ).fetchone()
        assert row is not None

    def test_set_sqlite_path_noop_when_pg_pool_wired(self):
        """set_sqlite_path is a no-op when PG pool is already set."""
        manager = StateManager()
        pool = MagicMock()
        manager.set_connection_pool(pool)

        manager.set_sqlite_path("/tmp/should_not_matter.db")
        assert manager._pool is pool


# ---------------------------------------------------------------------------
# update_state_data cross-instance
# ---------------------------------------------------------------------------


class TestUpdateStateCrossSQLite:
    """update_state_data writes are visible across SQLite-backed instances."""

    def test_update_visible_on_other_instance(self, db_path):
        """update_state_data on one instance is read by another."""
        manager_a = make_manager(db_path)
        manager_b = make_manager(db_path)

        token = manager_a.create_state({"step": 1})
        assert (
            manager_a.update_state_data(token, {"step": 2, "code_verifier": "v"})
            is True
        )

        result = manager_b.validate_state(token)
        assert result == {"step": 2, "code_verifier": "v"}

    def test_update_unknown_token_returns_false(self, db_path):
        """update_state_data on unknown token returns False."""
        manager = make_manager(db_path)
        assert manager.update_state_data("no-such-token", {"x": 1}) is False


# ---------------------------------------------------------------------------
# FakePool: real SQLite backend exercising PG code paths
# ---------------------------------------------------------------------------
# SQL translations applied by FakeCursor:
#   %s          -> ?
#   NOW()       -> current ISO timestamp (at execute time)
#   RETURNING x -> handled by SELECT-then-DELETE split
# cursor(row_factory=dict_row) returns dict rows keyed by column name.
# This is a faithful driver stand-in — writes actually persist in SQLite.
# ---------------------------------------------------------------------------


def _translate_pg_sql(sql: str, now_str: str) -> str:
    """Translate PG-style SQL to SQLite-compatible SQL.

    now_str must use space separator (str(datetime)) not isoformat (T),
    because _pg_create passes a datetime object to SQLite which stores
    it with a space separator — string comparisons must use the same format.
    """
    sql = sql.replace("%s", "?")
    sql = sql.replace("NOW()", f"'{now_str}'")
    return sql


def _strip_returning(sql: str) -> tuple:
    """Split 'DELETE ... RETURNING col' into (delete_sql, returning_col).

    Returns (original_sql, None) if no RETURNING clause.
    """
    upper = sql.upper()
    idx = upper.find(" RETURNING ")
    if idx == -1:
        return sql, None
    returning_col = sql[idx + len(" RETURNING ") :].strip()
    delete_sql = sql[:idx]
    return delete_sql, returning_col


class _DictCursor:
    """Context-manager cursor returning dict rows (simulates psycopg3 dict_row).

    Handles RETURNING by splitting DELETE into SELECT-then-DELETE.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._rows: list = []

    def execute(self, sql: str, params=None) -> "_DictCursor":
        from datetime import datetime, timezone

        now_str = str(datetime.now(timezone.utc))
        sql = _translate_pg_sql(sql, now_str)
        delete_sql, returning_col = _strip_returning(sql)

        if returning_col is not None:
            # Build equivalent SELECT to get rows before deletion
            # Transform: DELETE FROM t WHERE ... -> SELECT * FROM t WHERE ...
            where_part = delete_sql.upper().split("WHERE", 1)
            if len(where_part) == 2:
                table_part = delete_sql.split("FROM", 1)[1].split("WHERE")[0].strip()
                where_clause = delete_sql.split("WHERE", 1)[1].strip()
                select_sql = f"SELECT * FROM {table_part} WHERE {where_clause}"
                cur = self._conn.execute(
                    select_sql, params if params is not None else ()
                )
                col_names = [d[0] for d in cur.description] if cur.description else []
                self._rows = [dict(zip(col_names, row)) for row in cur.fetchall()]
            # Now delete
            self._conn.execute(delete_sql, params if params is not None else ())
        else:
            cur = self._conn.execute(sql, params if params is not None else ())
            col_names = [d[0] for d in cur.description] if cur.description else []
            self._rows = [dict(zip(col_names, row)) for row in cur.fetchall()]

        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self) -> "_DictCursor":
        return self

    def __exit__(self, *args) -> None:
        pass


class _DirectCursor:
    """Plain cursor (for non-dict-row calls — INSERT, UPDATE, plain DELETE)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._sqlite_cur: object = None
        self.rowcount: int = 0

    def execute(self, sql: str, params=None) -> "_DirectCursor":
        from datetime import datetime, timezone

        now_str = str(datetime.now(timezone.utc))
        sql = _translate_pg_sql(sql, now_str)
        delete_sql, returning_col = _strip_returning(sql)

        if returning_col is not None:
            # SELECT first, then DELETE; stash rows for fetchall()
            where_part = delete_sql.upper().split("WHERE", 1)
            if len(where_part) == 2:
                table_part = delete_sql.split("FROM", 1)[1].split("WHERE")[0].strip()
                where_clause = delete_sql.split("WHERE", 1)[1].strip()
                select_sql = f"SELECT * FROM {table_part} WHERE {where_clause}"
                cur = self._conn.execute(
                    select_sql, params if params is not None else ()
                )
                self._rows = cur.fetchall()
            self._sqlite_cur = self._conn.execute(
                delete_sql, params if params is not None else ()
            )
            self.rowcount = getattr(self._sqlite_cur, "rowcount", 0)
        else:
            self._rows = []
            self._sqlite_cur = self._conn.execute(
                sql, params if params is not None else ()
            )
            self.rowcount = getattr(self._sqlite_cur, "rowcount", 0)

        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows if self._rows else []


class _FakeConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params=None) -> _DirectCursor:
        return _DirectCursor(self._conn).execute(sql, params)

    def cursor(self, row_factory=None) -> _DictCursor:
        """Return dict cursor (row_factory is ignored; we always return dicts)."""
        return _DictCursor(self._conn)

    def commit(self) -> None:
        self._conn.commit()


class _FakePool:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def connection(self):
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield _FakeConnection(self._conn)

        return _ctx()


def _make_pg_schema(conn: sqlite3.Connection) -> None:
    """Create oidc_state_tokens table in SQLite for PG-path tests."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oidc_state_tokens (
            state_token TEXT PRIMARY KEY,
            state_data  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()


@pytest.fixture()
def pg_pool():
    """Shared SQLite-backed FakePool for PG-path tests."""
    conn = sqlite3.connect(":memory:")
    _make_pg_schema(conn)
    pool = _FakePool(conn)
    yield pool, conn
    conn.close()


def make_pg_manager(pool) -> StateManager:
    """Create a StateManager wired to the FakePool."""
    mgr = StateManager()
    mgr.set_connection_pool(pool)
    return mgr


# ---------------------------------------------------------------------------
# PG backend tests (DEFECT 2)
# ---------------------------------------------------------------------------


class TestPGBackend:
    """Exercises the PG code paths (_pg_create, _pg_validate, _pg_update,
    _pg_prune) via a real SQLite FakePool — faithful writes, no silent no-ops.
    """

    def test_pg_create_then_validate_returns_data(self, pg_pool):
        """PG create+validate round-trip returns the stored data."""
        pool, _ = pg_pool
        mgr = make_pg_manager(pool)

        token = mgr.create_state({"code_verifier": "pg-verifier", "redirect_to": "/x"})
        result = mgr.validate_state(token)

        assert result == {"code_verifier": "pg-verifier", "redirect_to": "/x"}

    def test_pg_validate_is_single_use(self, pg_pool):
        """PG validate deletes row atomically — second call returns None."""
        pool, _ = pg_pool
        mgr = make_pg_manager(pool)

        token = mgr.create_state({"code_verifier": "once"})
        first = mgr.validate_state(token)
        assert first is not None

        second = mgr.validate_state(token)
        assert second is None

    def test_pg_cross_instance_sharing(self, pg_pool):
        """State written by pg_manager_a is readable by pg_manager_b."""
        pool, _ = pg_pool
        mgr_a = make_pg_manager(pool)
        mgr_b = make_pg_manager(pool)

        token = mgr_a.create_state({"code_verifier": "shared"})
        result = mgr_b.validate_state(token)
        assert result == {"code_verifier": "shared"}

    def test_pg_expired_state_returns_none(self, pg_pool):
        """PG validate returns None for TTL-expired token."""
        pool, conn = pg_pool
        mgr = make_pg_manager(pool)

        token = mgr.create_state({"code_verifier": "will-expire"})
        # Backdate expires_at using space-separator (str()) so it sorts before
        # the space-separator NOW() substitution in _translate_pg_sql.
        past = str(datetime.now(timezone.utc) - timedelta(seconds=10))
        conn.execute(
            "UPDATE oidc_state_tokens SET expires_at = ? WHERE state_token = ?",
            (past, token),
        )
        conn.commit()

        assert mgr.validate_state(token) is None

    def test_pg_unknown_token_returns_none(self, pg_pool):
        """PG validate returns None for unknown token."""
        pool, _ = pg_pool
        mgr = make_pg_manager(pool)
        assert mgr.validate_state("nonexistent-pg-token") is None

    def test_pg_prune_removes_expired_leaves_valid(self, pg_pool):
        """PG prune_expired deletes expired rows only."""
        pool, conn = pg_pool
        mgr = make_pg_manager(pool)

        valid_token = mgr.create_state({"code_verifier": "valid"})
        expired_token = mgr.create_state({"code_verifier": "expired"})

        # Space-separator matches _translate_pg_sql NOW() substitution format.
        past = str(datetime.now(timezone.utc) - timedelta(seconds=10))
        conn.execute(
            "UPDATE oidc_state_tokens SET expires_at = ? WHERE state_token = ?",
            (past, expired_token),
        )
        conn.commit()

        deleted = mgr.prune_expired()
        assert deleted == 1

        assert mgr.validate_state(valid_token) is not None
        assert mgr.validate_state(expired_token) is None

    def test_pg_update_visible_on_other_instance(self, pg_pool):
        """PG update_state_data write is visible to another manager instance."""
        pool, _ = pg_pool
        mgr_a = make_pg_manager(pool)
        mgr_b = make_pg_manager(pool)

        token = mgr_a.create_state({"step": 1})
        assert mgr_a.update_state_data(token, {"step": 2, "code_verifier": "v"}) is True

        result = mgr_b.validate_state(token)
        assert result == {"step": 2, "code_verifier": "v"}


# ---------------------------------------------------------------------------
# DEFECT 1: prune scheduler uses wired instance, not a fresh one
# ---------------------------------------------------------------------------


class TestPruneUsesWiredInstance:
    """_safe_prune_oidc_state_tokens must prune via oidc_routes.state_manager
    (the PG-wired instance in cluster mode), not a freshly constructed
    StateManager() that has no PG pool.
    """

    def test_prune_scheduler_uses_oidc_routes_manager(self, pg_pool, tmp_path):
        """DataRetentionScheduler prunes the PG store, not a fresh SQLite one."""
        from code_indexer.server.auth.oidc import routes as oidc_routes
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        pool, conn = pg_pool
        wired_mgr = make_pg_manager(pool)

        # Plant an expired token in the PG store.
        # Space-separator matches _translate_pg_sql NOW() substitution format.
        expired_token = wired_mgr.create_state({"code_verifier": "pg-expire"})
        past = str(datetime.now(timezone.utc) - timedelta(seconds=10))
        conn.execute(
            "UPDATE oidc_state_tokens SET expires_at = ? WHERE state_token = ?",
            (past, expired_token),
        )
        conn.commit()

        # Temporarily wire oidc_routes.state_manager to our PG-wired manager
        original_mgr = oidc_routes.state_manager
        oidc_routes.state_manager = wired_mgr
        try:
            scheduler = DataRetentionScheduler.__new__(DataRetentionScheduler)
            deleted = scheduler._safe_prune_oidc_state_tokens()
        finally:
            oidc_routes.state_manager = original_mgr

        # The expired PG row must have been pruned
        assert deleted == 1

    def test_prune_scheduler_noop_when_state_manager_none(self):
        """_safe_prune_oidc_state_tokens is a no-op when oidc_routes.state_manager is None."""
        from code_indexer.server.auth.oidc import routes as oidc_routes
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        original_mgr = oidc_routes.state_manager
        oidc_routes.state_manager = None
        try:
            scheduler = DataRetentionScheduler.__new__(DataRetentionScheduler)
            deleted = scheduler._safe_prune_oidc_state_tokens()
        finally:
            oidc_routes.state_manager = original_mgr

        assert deleted == 0


# ---------------------------------------------------------------------------
# Real-PostgreSQL tests — gated on TEST_POSTGRES_DSN (skipped when unset)
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

_REAL_PG_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

_REAL_PG_SKIP = pytest.mark.skipif(
    not _REAL_PG_DSN,
    reason="TEST_POSTGRES_DSN not set — real-PostgreSQL test skipped",
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS oidc_state_tokens (
    state_token TEXT PRIMARY KEY,
    state_data  TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);
"""


@pytest.fixture()
def real_pg_pool():
    """Real psycopg3 ConnectionPool against TEST_POSTGRES_DSN.

    Skipped automatically when TEST_POSTGRES_DSN is unset (the skipif marker
    fires before the fixture body runs).

    Pool cleanup is guaranteed via try/finally so teardown always runs even
    if the table-setup step raises before yield.
    """
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    pool = ConnectionPool(_REAL_PG_DSN, min_size=1, max_size=4, name="oidc-1224-test")
    try:
        # Ensure the table exists (idempotent — safe if migration already ran).
        with pool.connection() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()
        yield pool
    finally:
        try:
            pool._pool.close()  # type: ignore[attr-defined]
        except Exception:
            _logger.warning("oidc-1224-test pool teardown failed", exc_info=True)


def _make_real_pg_manager(pool: object) -> StateManager:
    """Return a StateManager wired to a real PG pool."""
    mgr = StateManager()
    mgr.set_connection_pool(pool)
    return mgr


@_REAL_PG_SKIP
class TestRealPGBackend:
    """Exercises StateManager against a REAL PostgreSQL database.

    These tests skip automatically when TEST_POSTGRES_DSN is unset so they
    never block fast-automation.sh.  On CI runs that supply a real PG instance
    (server-fast / staging) they provide genuine coverage including the
    DELETE...RETURNING single-use atomicity guarantee that a SQLite FakePool
    cannot prove.
    """

    def test_cross_instance_create_validate(self, real_pg_pool):
        """State written by instance A is visible to instance B over real PG."""
        mgr_a = _make_real_pg_manager(real_pg_pool)
        mgr_b = _make_real_pg_manager(real_pg_pool)

        data = {"code_verifier": "real-pg-pkce", "redirect_to": "/dashboard"}
        token = mgr_a.create_state(data)

        result = mgr_b.validate_state(token)
        assert result == data

    def test_single_use_atomicity_under_concurrency(self, real_pg_pool):
        """DELETE...RETURNING guarantees exactly one concurrent validate wins.

        Two threads call validate_state on the same token simultaneously.
        Exactly one must receive the state data; all others must receive None.
        This is the property the DELETE...RETURNING guarantees and the only test
        that genuinely proves it — a SELECT-then-DELETE fake tests the wrong
        (non-atomic) shape.
        """
        mgr = _make_real_pg_manager(real_pg_pool)
        token = mgr.create_state({"code_verifier": "concurrency-test"})

        results: list = []
        barrier = threading.Barrier(2)

        def _validate() -> None:
            barrier.wait()  # both threads start validate at the same instant
            results.append(mgr.validate_state(token))

        t1 = threading.Thread(target=_validate)
        t2 = threading.Thread(target=_validate)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1, (
            f"Expected exactly 1 winner, got {len(non_none)}: {results}"
        )
        assert non_none[0]["code_verifier"] == "concurrency-test"

    def test_expired_state_returns_none(self, real_pg_pool):
        """Expired token (expires_at in the past) returns None from validate."""
        mgr = _make_real_pg_manager(real_pg_pool)
        token = mgr.create_state({"code_verifier": "expire-pg"})

        # Backdate the row directly so it is already expired.
        with real_pg_pool.connection() as conn:
            conn.execute(
                "UPDATE oidc_state_tokens"
                " SET expires_at = NOW() - INTERVAL '1 second'"
                " WHERE state_token = %s",
                (token,),
            )
            conn.commit()

        assert mgr.validate_state(token) is None

    def test_prune_expired_removes_only_expired_rows(self, real_pg_pool):
        """prune_expired deletes only rows whose expires_at <= NOW()."""
        mgr = _make_real_pg_manager(real_pg_pool)

        valid_token = mgr.create_state({"code_verifier": "keep-me"})
        expired_token = mgr.create_state({"code_verifier": "delete-me"})

        # Backdate only the expired token.
        with real_pg_pool.connection() as conn:
            conn.execute(
                "UPDATE oidc_state_tokens"
                " SET expires_at = NOW() - INTERVAL '1 second'"
                " WHERE state_token = %s",
                (expired_token,),
            )
            conn.commit()

        deleted = mgr.prune_expired()
        assert deleted >= 1  # at least our expired row was removed

        # The valid token must still validate successfully.
        result = mgr.validate_state(valid_token)
        assert result is not None
        assert result["code_verifier"] == "keep-me"

        # The expired token must be gone (already deleted by prune).
        assert mgr.validate_state(expired_token) is None
