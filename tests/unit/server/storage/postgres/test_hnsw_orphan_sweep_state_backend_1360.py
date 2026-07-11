"""Unit tests for HNSWOrphanSweepStatePostgresBackend (Story #1360, Epic #1333 S3).

Mirrors the mocked-pool convention used across
test_remaining_backends_part2.py / test_temporal_metadata_postgres_backend.py:
a MagicMock connection pool exercises SQL text + parameterization + psycopg
v3 API correctness (conn.execute, %s placeholders -- never ?), matching the
project's faithful-DB-mock discipline (feedback_faithful_db_mocks).

The migration file's DDL is checked separately (static, no live PG needed)
mirroring test_consumer_rate_limit_migration_1332.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _make_pool(fetchone_return=None) -> MagicMock:
    """MagicMock mimicking a psycopg v3 ConnectionPool context-manager."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> MagicMock:
    return pool.connection.return_value.__enter__.return_value  # type: ignore[no-any-return]


_DEFAULT_ROW = (1, 1, None, None, 0, 0, 0, 0, 0, None, 0)


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
    _MIGRATION_FILE = _SQL_DIR / "035_hnsw_orphan_repair_sweep_state.sql"

    def test_migration_file_exists(self) -> None:
        assert self._MIGRATION_FILE.exists()

    def test_migration_creates_table_if_not_exists(self) -> None:
        content = self._MIGRATION_FILE.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS hnsw_orphan_sweep_state" in content
        assert "DROP TABLE" not in content
        assert "DROP COLUMN" not in content


class TestHNSWOrphanSweepStatePostgresBackend:
    def test_get_state_initialises_singleton_when_absent(self) -> None:
        from code_indexer.server.storage.postgres.hnsw_orphan_sweep_state_backend import (
            HNSWOrphanSweepStatePostgresBackend,
        )

        pool = _make_pool()
        conn = _get_conn(pool)
        conn.execute.return_value.fetchone.side_effect = [None, _DEFAULT_ROW]

        backend = HNSWOrphanSweepStatePostgresBackend(pool)
        state = backend.get_state()

        assert state["pass_id"] == 1
        assert state["last_completed_key"] is None
        assert state["total_orphans_repaired_lifetime"] == 0
        calls = conn.execute.call_args_list
        insert_calls = [c for c in calls if "INSERT" in str(c[0][0]).upper()]
        assert len(insert_calls) >= 1

    def test_get_state_returns_existing_row_without_insert(self) -> None:
        from code_indexer.server.storage.postgres.hnsw_orphan_sweep_state_backend import (
            HNSWOrphanSweepStatePostgresBackend,
        )

        pool = _make_pool(fetchone_return=_DEFAULT_ROW)
        backend = HNSWOrphanSweepStatePostgresBackend(pool)

        state = backend.get_state()
        assert state["pass_id"] == 1

        conn = _get_conn(pool)
        calls = conn.execute.call_args_list
        insert_calls = [c for c in calls if "INSERT" in str(c[0][0]).upper()]
        assert insert_calls == []

    def test_record_item_processed_uses_percent_s_placeholders(self) -> None:
        from code_indexer.server.storage.postgres.hnsw_orphan_sweep_state_backend import (
            HNSWOrphanSweepStatePostgresBackend,
        )

        pool = _make_pool(fetchone_return=_DEFAULT_ROW)
        backend = HNSWOrphanSweepStatePostgresBackend(pool)
        backend.record_item_processed("golden:a:1", "repaired")

        conn = _get_conn(pool)
        update_calls = [
            c for c in conn.execute.call_args_list if "UPDATE" in str(c[0][0]).upper()
        ]
        assert update_calls, "expected an UPDATE statement"
        sql = update_calls[-1][0][0]
        assert "%s" in sql
        assert "?" not in sql
        assert "pass_orphaned_found" in sql
        assert "pass_repaired" in sql

    def test_record_item_processed_unknown_outcome_raises_without_sql(self) -> None:
        from code_indexer.server.storage.postgres.hnsw_orphan_sweep_state_backend import (
            HNSWOrphanSweepStatePostgresBackend,
        )
        import pytest

        pool = _make_pool(fetchone_return=_DEFAULT_ROW)
        backend = HNSWOrphanSweepStatePostgresBackend(pool)

        with pytest.raises(ValueError):
            backend.record_item_processed("golden:a:1", "bogus")

        conn = _get_conn(pool)
        update_calls = [
            c for c in conn.execute.call_args_list if "UPDATE" in str(c[0][0]).upper()
        ]
        assert update_calls == []

    def test_complete_pass_uses_percent_s_and_increments_pass_id(self) -> None:
        from code_indexer.server.storage.postgres.hnsw_orphan_sweep_state_backend import (
            HNSWOrphanSweepStatePostgresBackend,
        )

        pool = _make_pool(fetchone_return=_DEFAULT_ROW)
        backend = HNSWOrphanSweepStatePostgresBackend(pool)
        backend.complete_pass()

        conn = _get_conn(pool)
        update_calls = [
            c for c in conn.execute.call_args_list if "UPDATE" in str(c[0][0]).upper()
        ]
        assert update_calls, "expected an UPDATE statement"
        sql = update_calls[-1][0][0]
        assert "%s" in sql
        assert "pass_id = pass_id + 1" in sql
        assert "total_orphans_repaired_lifetime" in sql
