"""
TDD tests for Bug #1072 Chunk 1: private_key column on both SSH key backends.

Tests assert that:
1. PG backend create_key accepts private_key and includes it in INSERT params.
2. PG backend get_key returns private_key in the result dict.
3. PG backend list_keys returns private_key in each result dict.
4. Backward-compat: omitting private_key passes None in INSERT; get/list return None.
5. SQLite backend round-trips private_key through a real temp DB.
6. Both backends return dicts with the same keys (symmetry).

Storage-layer ONLY - no SSHKeyManager, no lifespan, no sync service.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# PG mock-pool helpers (mirrors test_remaining_backends_part1.py style)
# ---------------------------------------------------------------------------


def _make_pg_pool(fetchone=None, fetchall=None) -> MagicMock:
    """Return a MagicMock pool that mimics psycopg v3 ConnectionPool."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone
    cursor.fetchall.return_value = fetchall if fetchall is not None else []
    cursor.rowcount = 1
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_pg_conn(pool: MagicMock) -> MagicMock:
    """Return the mock connection from the pool context manager."""
    return pool.connection.return_value.__enter__.return_value  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# SQLite fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> Generator:
    """Create SSHKeysSqliteBackend backed by a real temp SQLite database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import SSHKeysSqliteBackend

    db_path = tmp_path / "test_ssh_keys.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    backend = SSHKeysSqliteBackend(str(db_path))
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# PG backend — private_key in INSERT
# ---------------------------------------------------------------------------


class TestPGBackendPrivateKey:
    def test_create_key_with_private_key_includes_value_in_insert(self) -> None:
        """
        When create_key is called with private_key='ENCRYPTED_BLOB',
        the INSERT params must contain 'ENCRYPTED_BLOB'.
        """
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        pool = _make_pg_pool()
        backend = SSHKeysPostgresBackend(pool)
        backend.create_key(
            name="my-key",
            fingerprint="AA:BB",
            key_type="ed25519",
            private_path="/path/priv",
            public_path="/path/pub",
            private_key="ENCRYPTED_BLOB",
        )

        conn = _get_pg_conn(pool)
        # The INSERT call is the first execute call
        insert_call = conn.execute.call_args_list[0]
        sql, params = insert_call[0]
        assert "INSERT INTO ssh_keys" in sql
        assert "ENCRYPTED_BLOB" in params

    def test_create_key_without_private_key_passes_none(self) -> None:
        """
        Backward-compat: omitting private_key must pass None in INSERT params.
        """
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        pool = _make_pg_pool()
        backend = SSHKeysPostgresBackend(pool)
        backend.create_key(
            name="my-key",
            fingerprint="AA:BB",
            key_type="ed25519",
            private_path="/path/priv",
            public_path="/path/pub",
        )

        conn = _get_pg_conn(pool)
        insert_call = conn.execute.call_args_list[0]
        _sql, params = insert_call[0]
        # private_key should appear in params as None (last param before or after others)
        assert None in params

    def test_get_key_returns_private_key_in_dict(self) -> None:
        """
        get_key must return a dict that includes 'private_key': 'ENCRYPTED_BLOB'
        when the DB row contains that value.
        """
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        # Row order: name, fingerprint, key_type, private_path, public_path,
        #            public_key, email, description, created_at, imported_at,
        #            is_imported, private_key  (private_key LAST)
        row = (
            "my-key",  # name
            "AA:BB",  # fingerprint
            "ed25519",  # key_type
            "/path/priv",  # private_path
            "/path/pub",  # public_path
            "pubkeydata",  # public_key
            None,  # email
            None,  # description
            "2026-01-01",  # created_at
            None,  # imported_at
            False,  # is_imported
            "ENCRYPTED_BLOB",  # private_key (LAST)
        )
        # _get_hosts_for_key will be called — return empty list
        pool = _make_pg_pool(fetchone=row, fetchall=[])
        backend = SSHKeysPostgresBackend(pool)
        result = backend.get_key("my-key")

        assert result is not None
        assert "private_key" in result
        assert result["private_key"] == "ENCRYPTED_BLOB"

    def test_get_key_backward_compat_private_key_none(self) -> None:
        """
        get_key returns 'private_key': None when DB has NULL in that column.
        """
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        row = (
            "my-key",
            "AA:BB",
            "ed25519",
            "/path/priv",
            "/path/pub",
            None,
            None,
            None,
            "2026-01-01",
            None,
            False,
            None,  # private_key = NULL
        )
        pool = _make_pg_pool(fetchone=row, fetchall=[])
        backend = SSHKeysPostgresBackend(pool)
        result = backend.get_key("my-key")

        assert result is not None
        assert "private_key" in result
        assert result["private_key"] is None

    def test_list_keys_returns_private_key_in_each_row(self) -> None:
        """
        list_keys must include 'private_key' in every returned dict.
        """
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        rows = [
            (
                "key-1",
                "AA:BB",
                "ed25519",
                "/path/priv",
                "/path/pub",
                None,
                None,
                None,
                "2026-01-01",
                None,
                False,
                "BLOB_1",  # private_key LAST
            ),
            (
                "key-2",
                "CC:DD",
                "rsa",
                "/path/priv2",
                "/path/pub2",
                None,
                None,
                None,
                "2026-01-01",
                None,
                False,
                None,  # private_key NULL
            ),
        ]
        pool = _make_pg_pool(fetchall=rows)
        backend = SSHKeysPostgresBackend(pool)
        results = backend.list_keys()

        assert len(results) == 2
        assert "private_key" in results[0]
        assert results[0]["private_key"] == "BLOB_1"
        assert "private_key" in results[1]
        assert results[1]["private_key"] is None


# ---------------------------------------------------------------------------
# SQLite backend — real temp DB round-trip
# ---------------------------------------------------------------------------


class TestSQLiteBackendPrivateKey:
    def test_create_and_get_key_with_private_key(self, sqlite_backend) -> None:
        """
        Round-trip: create_key(private_key='ENCRYPTED_BLOB') and get_key
        returns dict with private_key='ENCRYPTED_BLOB'.
        """
        sqlite_backend.create_key(
            name="my-key",
            fingerprint="AA:BB",
            key_type="ed25519",
            private_path="/path/priv",
            public_path="/path/pub",
            private_key="ENCRYPTED_BLOB",
        )
        result = sqlite_backend.get_key("my-key")

        assert result is not None
        assert "private_key" in result
        assert result["private_key"] == "ENCRYPTED_BLOB"

    def test_create_and_list_keys_with_private_key(self, sqlite_backend) -> None:
        """
        list_keys after create_key includes private_key in returned dicts.
        """
        sqlite_backend.create_key(
            name="my-key",
            fingerprint="AA:BB",
            key_type="ed25519",
            private_path="/path/priv",
            public_path="/path/pub",
            private_key="ENCRYPTED_BLOB",
        )
        results = sqlite_backend.list_keys()

        assert len(results) == 1
        assert "private_key" in results[0]
        assert results[0]["private_key"] == "ENCRYPTED_BLOB"

    def test_backward_compat_no_private_key(self, sqlite_backend) -> None:
        """
        Existing callers that don't pass private_key get None in returned dict.
        """
        sqlite_backend.create_key(
            name="old-key",
            fingerprint="EE:FF",
            key_type="rsa",
            private_path="/path/priv",
            public_path="/path/pub",
        )
        result = sqlite_backend.get_key("old-key")

        assert result is not None
        assert "private_key" in result
        assert result["private_key"] is None

        listed = sqlite_backend.list_keys()
        assert len(listed) == 1
        assert listed[0]["private_key"] is None


# ---------------------------------------------------------------------------
# Symmetry: both backends return dicts with the same keys
# ---------------------------------------------------------------------------


class TestBackendSymmetry:
    def test_pg_and_sqlite_return_same_dict_keys(self, sqlite_backend) -> None:
        """
        The dict keys returned by get_key on PG backend and SQLite backend
        must be identical (including 'private_key').
        """
        from code_indexer.server.storage.postgres.ssh_keys_backend import (
            SSHKeysPostgresBackend,
        )

        # SQLite result
        sqlite_backend.create_key(
            name="sym-key",
            fingerprint="GG:HH",
            key_type="ed25519",
            private_path="/path/priv",
            public_path="/path/pub",
            private_key="BLOB",
        )
        sqlite_result = sqlite_backend.get_key("sym-key")
        assert sqlite_result is not None

        # PG result (mocked row matching new schema)
        row = (
            "sym-key",
            "GG:HH",
            "ed25519",
            "/path/priv",
            "/path/pub",
            None,
            None,
            None,
            "2026-01-01",
            None,
            False,
            "BLOB",  # private_key LAST
        )
        pool = _make_pg_pool(fetchone=row, fetchall=[])
        pg_result = SSHKeysPostgresBackend(pool).get_key("sym-key")
        assert pg_result is not None

        # Both must have the same keys
        assert set(sqlite_result.keys()) == set(pg_result.keys()), (
            f"Key mismatch: SQLite={set(sqlite_result.keys())} "
            f"PG={set(pg_result.keys())}"
        )
        assert "private_key" in sqlite_result
        assert "private_key" in pg_result
