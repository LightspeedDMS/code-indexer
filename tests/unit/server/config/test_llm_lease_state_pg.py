"""
Tests for Task #29: LlmLeaseStateManager PostgreSQL cluster support.

Validates that LlmLeaseStateManager.set_connection_pool() enables PG-backed
lease state storage via the existing cluster_secrets table, so all cluster
nodes share lease state and can read it with a common encryption key.

Uses real SQLite as stand-in for PostgreSQL to avoid mocking.
PG methods use %s placeholders translated to ? for SQLite compat via FakePool.
"""

import logging
import sqlite3
from contextlib import contextmanager
from typing import Generator

import pytest


# ---------------------------------------------------------------------------
# SQLite/FakePool helpers (same pattern as test_rate_limiter_cluster.py)
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class FakeConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params=None):
        sql = sql.replace("%s", "?")
        sql = sql.replace("EXCLUDED.", "excluded.")
        if params is not None:
            return FakeCursor(self._conn.execute(sql, params))
        return FakeCursor(self._conn.execute(sql))

    def commit(self):
        self._conn.commit()


class FakePool:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Generator[FakeConnection, None, None]:
        yield FakeConnection(self._conn)


def _create_cluster_secrets_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cluster_secrets (
            key_name TEXT PRIMARY KEY,
            key_value TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _seed_jwt_secret(
    conn: sqlite3.Connection, secret: str = "test-jwt-secret-value"
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cluster_secrets (key_name, key_value) VALUES ('jwt_secret', ?)",
        (secret,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_conn():
    conn = sqlite3.connect(":memory:")
    _create_cluster_secrets_table(conn)
    _seed_jwt_secret(conn)
    yield conn
    conn.close()


@pytest.fixture
def pool_and_conn(sqlite_conn):
    pool = FakePool(sqlite_conn)
    return pool, sqlite_conn


@pytest.fixture
def cluster_conn_custom_secret():
    conn = sqlite3.connect(":memory:")
    _create_cluster_secrets_table(conn)
    _seed_jwt_secret(conn, "shared-cluster-jwt-secret")
    yield conn
    conn.close()


@pytest.fixture
def two_conns_different_secrets():
    conn1 = sqlite3.connect(":memory:")
    conn2 = sqlite3.connect(":memory:")
    _create_cluster_secrets_table(conn1)
    _seed_jwt_secret(conn1, "secret-A")
    _create_cluster_secrets_table(conn2)
    _seed_jwt_secret(conn2, "secret-B")
    yield conn1, conn2
    conn1.close()
    conn2.close()


def _make_manager_with_pool(tmp_path, pool_and_conn):
    from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager

    pool, conn = pool_and_conn
    manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    manager.set_connection_pool(pool)
    return manager, conn, pool


def _make_two_managers_with_shared_pool(tmp_path, sqlite_conn):
    from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager

    pool = FakePool(sqlite_conn)
    node1 = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    node1.set_connection_pool(pool)
    node2 = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    node2.set_connection_pool(pool)
    return node1, node2


# ---------------------------------------------------------------------------
# Test: set_connection_pool stores pool and logs
# ---------------------------------------------------------------------------


class TestSetConnectionPool:
    def test_pool_is_none_by_default(self, tmp_path) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager

        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        assert manager._pool is None

    def test_set_connection_pool_stores_pool_reference(
        self, tmp_path, pool_and_conn
    ) -> None:
        manager, _, pool = _make_manager_with_pool(tmp_path, pool_and_conn)
        assert manager._pool is pool

    def test_set_connection_pool_logs_cluster_mode(
        self, tmp_path, sqlite_conn, caplog
    ) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager

        pool = FakePool(sqlite_conn)
        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        with caplog.at_level(logging.INFO):
            manager.set_connection_pool(pool)
        assert any("cluster" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Test: save_state() in PG mode
# ---------------------------------------------------------------------------


class TestSaveStatePg:
    def test_save_inserts_row_in_cluster_secrets(self, tmp_path, pool_and_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, conn, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        state = LlmLeaseState(lease_id="lease-001", credential_id="cred-001")
        manager.save_state(state)

        row = conn.execute(
            "SELECT key_name FROM cluster_secrets WHERE key_name = 'llm_lease_state'"
        ).fetchone()
        assert row is not None

    def test_save_stores_non_empty_value(self, tmp_path, pool_and_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, conn, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        state = LlmLeaseState(lease_id="lease-002", credential_id="cred-002")
        manager.save_state(state)

        row = conn.execute(
            "SELECT key_value FROM cluster_secrets WHERE key_name = 'llm_lease_state'"
        ).fetchone()
        assert row is not None
        assert row[0] != ""

    def test_save_overwrites_existing_state(self, tmp_path, pool_and_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, conn, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(
            LlmLeaseState(lease_id="lease-old", credential_id="cred-old")
        )
        manager.save_state(
            LlmLeaseState(lease_id="lease-new", credential_id="cred-new")
        )

        row = conn.execute(
            "SELECT COUNT(*) FROM cluster_secrets WHERE key_name = 'llm_lease_state'"
        ).fetchone()
        assert row[0] == 1

    def test_save_does_not_write_file_in_pg_mode(self, tmp_path, pool_and_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(
            LlmLeaseState(lease_id="lease-003", credential_id="cred-003")
        )

        state_file = tmp_path / "llm_lease_state.json"
        assert not state_file.exists()


# ---------------------------------------------------------------------------
# Test: load_state() in PG mode
# ---------------------------------------------------------------------------


class TestLoadStatePg:
    def test_load_returns_none_when_no_row_in_pg(self, tmp_path, pool_and_conn) -> None:
        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        result = manager.load_state()
        assert result is None

    def test_load_returns_correct_lease_id(self, tmp_path, pool_and_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(
            LlmLeaseState(lease_id="lease-abc", credential_id="cred-xyz")
        )
        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.lease_id == "lease-abc"

    def test_load_returns_correct_credential_id(self, tmp_path, pool_and_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(
            LlmLeaseState(lease_id="lease-abc", credential_id="cred-xyz")
        )
        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.credential_id == "cred-xyz"

    def test_load_returns_correct_credential_type(
        self, tmp_path, pool_and_conn
    ) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(
            LlmLeaseState(
                lease_id="lease-1", credential_id="cred-1", credential_type="api_key"
            )
        )
        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.credential_type == "api_key"

    def test_load_default_credential_type_is_oauth(
        self, tmp_path, pool_and_conn
    ) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(LlmLeaseState(lease_id="lease-2", credential_id="cred-2"))
        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.credential_type == "oauth"


# ---------------------------------------------------------------------------
# Test: clear_state() in PG mode
# ---------------------------------------------------------------------------


class TestClearStatePg:
    def test_clear_removes_row_from_cluster_secrets(
        self, tmp_path, pool_and_conn
    ) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, conn, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(
            LlmLeaseState(lease_id="lease-del", credential_id="cred-del")
        )
        manager.clear_state()

        row = conn.execute(
            "SELECT key_name FROM cluster_secrets WHERE key_name = 'llm_lease_state'"
        ).fetchone()
        assert row is None

    def test_clear_when_no_state_does_not_raise(self, tmp_path, pool_and_conn) -> None:
        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.clear_state()

    def test_load_after_clear_returns_none(self, tmp_path, pool_and_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        manager, _, _ = _make_manager_with_pool(tmp_path, pool_and_conn)
        manager.save_state(LlmLeaseState(lease_id="lease-x", credential_id="cred-x"))
        manager.clear_state()
        assert manager.load_state() is None


# ---------------------------------------------------------------------------
# Test: cross-node state sharing
# ---------------------------------------------------------------------------


class TestCrossNodeSharing:
    def test_two_managers_share_pg_state(self, tmp_path, sqlite_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        node1, node2 = _make_two_managers_with_shared_pool(tmp_path, sqlite_conn)
        node1.save_state(
            LlmLeaseState(lease_id="shared-lease", credential_id="shared-cred")
        )
        loaded_on_node2 = node2.load_state()
        assert loaded_on_node2 is not None
        assert loaded_on_node2.lease_id == "shared-lease"

    def test_clear_on_one_node_visible_to_other(self, tmp_path, sqlite_conn) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseState

        node1, node2 = _make_two_managers_with_shared_pool(tmp_path, sqlite_conn)
        node1.save_state(LlmLeaseState(lease_id="lease-y", credential_id="cred-y"))
        node1.clear_state()
        assert node2.load_state() is None


# ---------------------------------------------------------------------------
# Test: encryption uses shared key in cluster mode
# ---------------------------------------------------------------------------


class TestClusterEncryptionKey:
    def test_state_saved_by_node1_readable_by_node2(
        self, tmp_path, cluster_conn_custom_secret
    ) -> None:
        from code_indexer.server.config.llm_lease_state import (
            LlmLeaseState,
            LlmLeaseStateManager,
        )

        pool = FakePool(cluster_conn_custom_secret)

        node1 = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        node1.set_connection_pool(pool)
        node2 = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        node2.set_connection_pool(pool)

        node1.save_state(
            LlmLeaseState(lease_id="cross-lease", credential_id="cross-cred")
        )
        loaded = node2.load_state()
        assert loaded is not None
        assert loaded.lease_id == "cross-lease"
        assert loaded.credential_id == "cross-cred"

    def test_different_jwt_secrets_produce_different_keys(
        self, tmp_path, two_conns_different_secrets
    ) -> None:
        from code_indexer.server.config.llm_lease_state import LlmLeaseStateManager

        conn1, conn2 = two_conns_different_secrets
        pool1 = FakePool(conn1)
        pool2 = FakePool(conn2)

        mgr1 = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        mgr1.set_connection_pool(pool1)
        mgr2 = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        mgr2.set_connection_pool(pool2)

        key1 = mgr1._cluster_encryption_key
        key2 = mgr2._cluster_encryption_key
        assert key1 != key2


# ---------------------------------------------------------------------------
# Test: standalone file mode unaffected
# ---------------------------------------------------------------------------


class TestStandaloneFileMode:
    def test_save_and_load_still_work_without_pool(self, tmp_path) -> None:
        from code_indexer.server.config.llm_lease_state import (
            LlmLeaseState,
            LlmLeaseStateManager,
        )

        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        state = LlmLeaseState(lease_id="file-lease", credential_id="file-cred")
        manager.save_state(state)
        loaded = manager.load_state()
        assert loaded is not None
        assert loaded.lease_id == "file-lease"

    def test_clear_state_still_works_without_pool(self, tmp_path) -> None:
        from code_indexer.server.config.llm_lease_state import (
            LlmLeaseState,
            LlmLeaseStateManager,
        )

        manager = LlmLeaseStateManager(server_dir_path=str(tmp_path))
        manager.save_state(LlmLeaseState(lease_id="file-x", credential_id="file-x"))
        manager.clear_state()
        assert manager.load_state() is None


# ---------------------------------------------------------------------------
# Test: lifespan wires LlmLeaseStateManager to cluster pool (structural)
# ---------------------------------------------------------------------------


class TestLifespanWiring:
    def test_lifespan_wires_llm_lease_state_manager_pool(self) -> None:
        import inspect

        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)
        assert "LlmLeaseStateManager" in source
        assert "set_connection_pool" in source
