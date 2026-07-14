"""
Unit tests for GoldenRepoMetadataPostgresBackend's registry-reconcile
circuit-breaker persistence methods (Bug #1382).

Mirrors the mocked-pool convention used across
test_golden_repo_metadata_postgres.py / test_hnsw_orphan_sweep_state_backend_1360.py:
a MagicMock connection pool exercises SQL text + parameterization + psycopg
v3 API correctness (conn.cursor()/conn.commit(), %s placeholders -- never
?), matching the project's faithful-DB-mock discipline
(feedback_faithful_db_mocks). No real PostgreSQL required.

The migration file's DDL is checked separately (static, no live PG needed),
mirroring test_consumer_rate_limit_migration_1332.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock


def _make_mock_pool(fetchone_return=None):
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = fetchone_return

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_pool = MagicMock()

    @contextmanager
    def _connection():
        yield mock_conn

    mock_pool.connection.side_effect = _connection

    return mock_pool, mock_conn, mock_cursor


class TestMigrationFile:
    _SQL_DIR = (
        Path(__file__).parents[5]
        / "src"
        / "code_indexer"
        / "server"
        / "storage"
        / "postgres"
        / "migrations"
        / "sql"
    )
    _MIGRATION_FILE = _SQL_DIR / "036_golden_repo_reconcile_breaker_state.sql"

    def test_migration_file_exists(self) -> None:
        assert self._MIGRATION_FILE.exists()

    def test_migration_creates_table_if_not_exists(self) -> None:
        content = self._MIGRATION_FILE.read_text(encoding="utf-8")
        assert (
            "CREATE TABLE IF NOT EXISTS golden_repo_reconcile_breaker_state" in content
        )
        assert "DROP TABLE" not in content
        assert "DROP COLUMN" not in content


class TestGoldenRepoMetadataPostgresBackendReconcileBreaker:
    def test_first_observation_inserts_and_returns_one(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(pool)

        count = backend.record_reconcile_breaker_observation("a,b,c")

        assert count == 1
        insert_calls = [
            c for c in cursor.execute.call_args_list if "INSERT" in str(c[0][0]).upper()
        ]
        assert len(insert_calls) == 1
        # psycopg v3 uses %s placeholders, never sqlite's "?".
        assert "?" not in str(insert_calls[0][0][0])
        conn.commit.assert_called()

    def test_matching_fingerprint_increments_count(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=("a,b,c", 2))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        count = backend.record_reconcile_breaker_observation("a,b,c")

        assert count == 3
        update_calls = [
            c for c in cursor.execute.call_args_list if "UPDATE" in str(c[0][0]).upper()
        ]
        assert len(update_calls) == 1
        assert "consecutive_count" in str(update_calls[0][0][0])

    def test_different_fingerprint_resets_count_to_one(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=("x,y,z", 5))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        count = backend.record_reconcile_breaker_observation("a,b,c")

        assert count == 1
