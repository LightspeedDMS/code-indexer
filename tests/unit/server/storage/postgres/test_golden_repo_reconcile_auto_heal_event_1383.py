"""
Unit tests for GoldenRepoMetadataPostgresBackend's registry-reconcile
auto-heal event persistence (GitHub Issue #1383).

Mirrors the mocked-pool convention used by
test_golden_repo_reconcile_breaker_state_1382.py: a MagicMock connection
pool exercises SQL text + parameterization + psycopg v3 API correctness
(conn.cursor()/conn.commit(), %s placeholders -- never ?), matching the
project's faithful-DB-mock discipline (feedback_faithful_db_mocks). No real
PostgreSQL required.

The migration file's DDL is checked separately (static, no live PG needed).
"""

from __future__ import annotations

import datetime as dt
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
    _MIGRATION_FILE = _SQL_DIR / "037_golden_repo_reconcile_auto_heal_event.sql"

    def test_migration_file_exists(self) -> None:
        assert self._MIGRATION_FILE.exists()

    def test_migration_creates_table_if_not_exists(self) -> None:
        content = self._MIGRATION_FILE.read_text(encoding="utf-8")
        assert (
            "CREATE TABLE IF NOT EXISTS golden_repo_reconcile_auto_heal_event"
            in content
        )
        assert "DROP TABLE" not in content
        assert "DROP COLUMN" not in content


class TestGoldenRepoMetadataPostgresBackendAutoHealEvent:
    def test_record_upserts_row(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        backend.record_reconcile_auto_heal_event(["a", "b"])

        assert cursor.execute.call_count == 1
        sql_text = str(cursor.execute.call_args_list[0][0][0])
        # psycopg v3 uses %s placeholders, never sqlite's "?".
        assert "?" not in sql_text
        conn.commit.assert_called()

    def test_get_returns_none_when_no_row(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        pool, conn, cursor = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(pool)

        assert backend.get_reconcile_auto_heal_event() is None

    def test_get_returns_parsed_event(self) -> None:
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        occurred_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        pool, conn, cursor = _make_mock_pool(fetchone_return=("a,b", occurred_at))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        event = backend.get_reconcile_auto_heal_event()

        assert event is not None
        assert event["removed_aliases"] == ["a", "b"]
        assert event["occurred_at"] == occurred_at
