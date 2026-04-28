"""
Unit tests for Bug #874 Story B: PostgreSQL DependencyMapTrackingPostgresBackend
extension — run_type + phase_timings_json columns.

Tests use a MagicMock connection pool (no real PostgreSQL required) following
the pattern established in test_remaining_backends_part2.py.

Coverage:
  TestInsertBehavior (3 tests):
    1. INSERT SQL names run_type/phase_timings_json with %s placeholders; params
       carry the correct values when kwargs are provided.
    2. Legacy call (no new kwargs) — same SQL shape; last two params are None.
    3. get_run_history result dicts include run_type and phase_timings_json keys
       with correct values from the row tuple.

  TestMigrationFile (1 test):
    4. 021_dependency_map_run_history.sql exists and contains:
       - CREATE TABLE IF NOT EXISTS dependency_map_run_history
       - run_type VARCHAR(16)
       - phase_timings_json JSONB
       - ADD COLUMN IF NOT EXISTS run_type guard
       - ADD COLUMN IF NOT EXISTS phase_timings_json guard
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from code_indexer.server.storage.postgres.dependency_map_tracking_backend import (
    DependencyMapTrackingPostgresBackend,
)


# ---------------------------------------------------------------------------
# Helpers shared across both classes
# ---------------------------------------------------------------------------


def _make_pool() -> MagicMock:
    """Return a MagicMock mimicking a psycopg ConnectionPool context-manager."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    cursor.rowcount = 1
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> MagicMock:
    # cast needed: MagicMock attribute chains return Any; value is the mock conn
    return cast(MagicMock, pool.connection.return_value.__enter__.return_value)


def _make_backend() -> tuple[MagicMock, DependencyMapTrackingPostgresBackend]:
    """Return (pool, backend) with precise types — no type: ignore needed."""
    pool = _make_pool()
    return pool, DependencyMapTrackingPostgresBackend(pool)


def _base_metrics() -> dict[str, object]:
    return {
        "timestamp": "2026-04-23T00:00:00+00:00",
        "domain_count": 3,
        "total_chars": 1000,
        "edge_count": 5,
        "zero_char_domains": 0,
        "repos_analyzed": 2,
        "repos_skipped": 0,
        "pass1_duration_s": 1.0,
        "pass2_duration_s": 2.0,
    }


def _extract_insert_call(conn: MagicMock) -> tuple[str, tuple]:
    """Return (sql_text, params) from the single INSERT conn.execute call."""
    insert_calls = [
        c for c in conn.execute.call_args_list if "INSERT" in str(c.args[0])
    ]
    assert len(insert_calls) == 1, (
        f"Expected 1 INSERT call, got {len(insert_calls)}: {conn.execute.call_args_list}"
    )
    return insert_calls[0].args[0], insert_calls[0].args[1]


# ---------------------------------------------------------------------------
# Class 1: INSERT / SELECT behaviour (3 tests)
# ---------------------------------------------------------------------------


class TestInsertBehavior:
    """INSERT SQL shape and get_run_history key coverage for new fields."""

    def test_insert_sql_and_params_include_new_columns_when_provided(
        self,
    ) -> None:
        """INSERT names run_type/phase_timings_json with %s; params carry values."""
        pool, backend = _make_backend()
        backend.record_run_metrics(
            _base_metrics(),
            run_type="delta",
            phase_timings_json='{"detect_s":1.5,"merge_s":2.5}',
        )

        sql, params = _extract_insert_call(_get_conn(pool))

        assert "run_type" in sql, f"run_type missing from INSERT SQL: {sql}"
        assert "phase_timings_json" in sql, (
            f"phase_timings_json missing from INSERT SQL: {sql}"
        )
        assert "%s" in sql, f"INSERT must use %s placeholders (psycopg v3): {sql}"
        assert "delta" in params, f"run_type='delta' not in params: {params}"
        assert '{"detect_s":1.5,"merge_s":2.5}' in params, (
            f"phase_timings_json value not in params: {params}"
        )

    def test_legacy_call_inserts_none_for_new_fields(self) -> None:
        """Legacy call (no new kwargs): SQL unchanged; last two params are None."""
        pool, backend = _make_backend()
        backend.record_run_metrics(_base_metrics())

        sql, params = _extract_insert_call(_get_conn(pool))

        assert "run_type" in sql, (
            f"run_type must appear in INSERT SQL for legacy call: {sql}"
        )
        assert "phase_timings_json" in sql, (
            f"phase_timings_json must appear in INSERT SQL for legacy call: {sql}"
        )
        assert "%s" in sql
        assert params[-2] is None, (
            f"Expected run_type=None at params[-2], got {params[-2]!r}"
        )
        assert params[-1] is None, (
            f"Expected phase_timings_json=None at params[-1], got {params[-1]!r}"
        )

    def test_get_run_history_returns_new_field_keys_with_correct_values(
        self,
    ) -> None:
        """get_run_history dicts include run_type and phase_timings_json from row."""
        pool, backend = _make_backend()
        fake_row = (
            1,
            "2026-04-23T00:00:00+00:00",
            3,
            1000,
            5,
            0,
            2,
            0,
            1.0,
            2.0,  # pass1_duration_s, pass2_duration_s
            "full",  # run_type
            '{"synth_s":10}',  # phase_timings_json
        )
        _get_conn(pool).execute.return_value.fetchall.return_value = [fake_row]

        rows = backend.get_run_history(limit=5)

        assert len(rows) == 1
        row = rows[0]
        assert row.get("run_type") == "full", f"run_type key wrong: {row}"
        assert row.get("phase_timings_json") == '{"synth_s":10}', (
            f"phase_timings_json key wrong: {row}"
        )


# ---------------------------------------------------------------------------
# Class 2: Migration file DDL validation (1 test)
# ---------------------------------------------------------------------------


class TestMigrationFile:
    """021_dependency_map_run_history.sql must contain exact DDL fragments."""

    def test_migration_021_contains_correct_column_type_ddl(self) -> None:
        """File exists with CREATE TABLE, VARCHAR(16), JSONB, ADD COLUMN IF NOT EXISTS."""
        sql_dir = (
            Path(__file__).parent.parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "storage"
            / "postgres"
            / "migrations"
            / "sql"
        )
        sql_file = sql_dir / "021_dependency_map_run_history.sql"
        assert sql_file.exists(), f"Migration file missing: {sql_file}"

        content = sql_file.read_text()

        assert "CREATE TABLE IF NOT EXISTS dependency_map_run_history" in content, (
            "Migration must contain CREATE TABLE IF NOT EXISTS dependency_map_run_history"
        )
        assert "run_type VARCHAR(16)" in content, "run_type must be typed VARCHAR(16)"
        assert "phase_timings_json JSONB" in content, (
            "phase_timings_json must be typed JSONB (project convention)"
        )
        assert "ADD COLUMN IF NOT EXISTS run_type" in content, (
            "Missing ADD COLUMN IF NOT EXISTS run_type idempotency guard"
        )
        assert "ADD COLUMN IF NOT EXISTS phase_timings_json" in content, (
            "Missing ADD COLUMN IF NOT EXISTS phase_timings_json idempotency guard"
        )
