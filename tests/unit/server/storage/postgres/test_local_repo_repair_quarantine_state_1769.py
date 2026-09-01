"""
Unit tests for GoldenRepoMetadataPostgresBackend's local-repo `cidx init`
repair failure quarantine persistence methods (Bug #1769).

Mirrors the mocked-pool convention used by
test_refresh_integrity_quarantine_state_1506.py: a MagicMock connection
pool exercises SQL text + parameterization + psycopg v3 API correctness
(conn.cursor()/conn.commit(), %s placeholders -- never sqlite's "?"),
matching the project's faithful-DB-mock discipline
(feedback_faithful_db_mocks). No real PostgreSQL required.

The migration file's DDL is checked separately (static, no live PG needed).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
    GoldenRepoMetadataPostgresBackend,
)

# postgres -> storage -> server -> unit -> tests -> <repo root>
_REPO_ROOT_PARENTS_UP = 5


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


class TestGoldenRepoMetadataPostgresBackendRecordValidationParity:
    """record_local_repo_repair_failure must reject empty golden_alias/
    detail IDENTICALLY on both backends -- see the sibling SQLite parity
    tests in
    tests/unit/server/storage/test_local_repo_repair_quarantine_state_1769.py."""

    def test_record_rejects_empty_golden_alias(self) -> None:
        pool, _conn, _cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        with pytest.raises(ValueError):
            backend.record_local_repo_repair_failure("", "some detail")

    def test_record_rejects_empty_detail(self) -> None:
        pool, _conn, _cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        with pytest.raises(ValueError):
            backend.record_local_repo_repair_failure("click-global", "")


class TestGoldenRepoMetadataPostgresBackendResetGetStateValidationParity:
    def test_reset_rejects_empty_alias(self) -> None:
        pool, _conn, _cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        with pytest.raises(ValueError):
            backend.reset_local_repo_repair_failure("")

    def test_get_state_rejects_empty_alias(self) -> None:
        pool, _conn, _cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        with pytest.raises(ValueError):
            backend.get_local_repo_repair_failure_state("")


class TestMigrationFile:
    _SQL_DIR = (
        Path(__file__).parents[_REPO_ROOT_PARENTS_UP]
        / "src"
        / "code_indexer"
        / "server"
        / "storage"
        / "postgres"
        / "migrations"
        / "sql"
    )
    _MIGRATION_FILE = _SQL_DIR / "050_local_repo_repair_quarantine_state.sql"

    def test_migration_file_exists(self) -> None:
        assert self._MIGRATION_FILE.exists()

    def test_migration_creates_table_if_not_exists(self) -> None:
        content = self._MIGRATION_FILE.read_text(encoding="utf-8")
        assert (
            "CREATE TABLE IF NOT EXISTS local_repo_repair_quarantine_state" in content
        )
        assert "DROP TABLE" not in content
        assert "DROP COLUMN" not in content


class TestGoldenRepoMetadataPostgresBackendRecordLocalRepoRepairFailure:
    def test_first_failure_inserts_and_returns_one(self) -> None:
        pool, conn, cursor = _make_mock_pool(fetchone_return=(1,))
        backend = GoldenRepoMetadataPostgresBackend(pool)

        count = backend.record_local_repo_repair_failure("click-global", "detail-1")

        assert count == 1
        assert cursor.execute.call_count == 1
        sql_text = str(cursor.execute.call_args_list[0][0][0]).upper()
        assert "INSERT" in sql_text
        assert "ON CONFLICT" in sql_text
        assert "RETURNING" in sql_text
        assert "?" not in sql_text
        params = cursor.execute.call_args_list[0][0][1]
        assert "click-global" in params
        assert "detail-1" in params
        conn.commit.assert_called()


class TestGoldenRepoMetadataPostgresBackendResetLocalRepoRepairFailure:
    def test_reset_issues_delete(self) -> None:
        pool, conn, cursor = _make_mock_pool()
        backend = GoldenRepoMetadataPostgresBackend(pool)

        backend.reset_local_repo_repair_failure("click-global")

        delete_calls = [
            c for c in cursor.execute.call_args_list if "DELETE" in str(c[0][0]).upper()
        ]
        assert len(delete_calls) == 1
        assert "?" not in str(delete_calls[0][0][0])
        assert delete_calls[0][0][1] == ("click-global",)
        conn.commit.assert_called()


class TestGoldenRepoMetadataPostgresBackendGetLocalRepoRepairFailureState:
    def test_get_state_returns_none_when_no_row(self) -> None:
        pool, conn, cursor = _make_mock_pool(fetchone_return=None)
        backend = GoldenRepoMetadataPostgresBackend(pool)

        assert backend.get_local_repo_repair_failure_state("click-global") is None
        select_calls = [
            c for c in cursor.execute.call_args_list if "SELECT" in str(c[0][0]).upper()
        ]
        assert len(select_calls) == 1
        assert "?" not in str(select_calls[0][0][0])
        assert select_calls[0][0][1] == ("click-global",)

    def test_get_state_returns_dict_when_row_present(self) -> None:
        pool, conn, cursor = _make_mock_pool(
            fetchone_return=(
                "click-global",
                3,
                "detail-3",
                "2026-01-01T00:00:00",
                "2026-01-02T00:00:00",
            )
        )
        backend = GoldenRepoMetadataPostgresBackend(pool)

        state = backend.get_local_repo_repair_failure_state("click-global")

        assert state == {
            "golden_alias": "click-global",
            "consecutive_failure_count": 3,
            "last_detail": "detail-3",
            "first_failed_at": "2026-01-01T00:00:00",
            "last_failed_at": "2026-01-02T00:00:00",
        }
