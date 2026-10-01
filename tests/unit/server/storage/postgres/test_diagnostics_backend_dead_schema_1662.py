"""
Regression tests for GitHub Issue #1662: DiagnosticsBackend's str-typed
return contract is provably false for PostgreSQL JSONB/TIMESTAMPTZ columns,
and DiagnosticsPostgresBackend's `_ensure_schema()` is a dead, drifting
second source of truth for the `diagnostic_results` schema.

Two things confirmed by investigation before writing this file:

1. The real migration (`storage/postgres/migrations/sql/001_initial_schema.sql`)
   declares `diagnostic_results.results_json` as JSONB and
   `diagnostic_results.run_at` as TIMESTAMPTZ. psycopg deserializes both to
   native Python objects (dict/list, datetime) before the row reaches
   application code -- never a str. `DiagnosticsBackend.load_all_results()` /
   `load_category_results()` previously declared `str` for both, which is
   provably false and defeats mypy's ability to catch a caller that
   naively assumes `str` (exactly the class of bug that slipped through
   review for Bug #1653).

2. `startup/service_init.py` always runs `MigrationRunner` (fail-fast on
   error) BEFORE `StorageFactory.create_backends()` constructs any
   PostgreSQL backend in `storage_mode=postgres` -- so
   `DiagnosticsPostgresBackend._ensure_schema()`'s own
   `CREATE TABLE IF NOT EXISTS diagnostic_results (... TEXT ... TEXT ...)`
   never actually runs against a real database in any live deployment
   (and if it somehow did, its TEXT/TEXT column types would silently
   drift from the migration's JSONB/TIMESTAMPTZ). This is the exact same
   dead/drifting-schema-source pattern Bug #1655 found and fixed in
   `wiki_cache_backend.py` (see
   tests/unit/server/storage/postgres/test_wiki_cache_backend_metadata_column_bug_1655.py)
   -- the fix mirrors that precedent: delete the dead block entirely
   rather than re-sync its (unreachable) column types.

Tests mock ConnectionPool (no real PostgreSQL required -- same pattern as
test_wiki_cache_backend_metadata_column_bug_1655.py: pool.connection() ->
conn context manager, conn.execute(sql, params) returns a cursor-like
mock).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[5]
    / "src"
    / "code_indexer"
    / "server"
    / "storage"
    / "postgres"
    / "migrations"
    / "sql"
    / "001_initial_schema.sql"
)


def _migration_diagnostic_results_columns() -> dict:
    """Parse the real migration file and return diagnostic_results's
    column name -> declared SQL type mapping.

    Ground truth for this bug: reads the actual on-disk migration SQL
    rather than a hardcoded expectation, so a future migration edit is
    caught by this same test.
    """
    assert _MIGRATION_PATH.is_file(), f"migration file not found: {_MIGRATION_PATH}"
    sql = _MIGRATION_PATH.read_text()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS diagnostic_results \((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert match is not None, "diagnostic_results CREATE TABLE not found in migration"
    body = match.group(1)
    columns = {}
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(("PRIMARY KEY", "--")):
            continue
        parts = line.split()
        columns[parts[0]] = parts[1]
    return columns


# ---------------------------------------------------------------------------
# Mock pool helpers (identical pattern to
# test_wiki_cache_backend_metadata_column_bug_1655.py)
# ---------------------------------------------------------------------------


def _make_mock_pool(
    fetchone_return: Any = None, fetchall_return: Any = None
) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.return_value = (
        fetchall_return if fetchall_return is not None else []
    )
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> Any:
    return pool.connection.return_value.__enter__.return_value


def _make_backend(pool: MagicMock) -> Any:
    from code_indexer.server.storage.postgres.diagnostics_backend import (
        DiagnosticsPostgresBackend,
    )

    return DiagnosticsPostgresBackend(pool)


def _all_executed_sql(pool: MagicMock) -> list:
    conn = _get_conn(pool)
    return [call.args[0] for call in conn.execute.call_args_list]


class TestMigrationGroundTruth:
    """Pin the real migration's diagnostic_results column types as ground
    truth -- results_json is JSONB, run_at is TIMESTAMPTZ, never TEXT."""

    def test_migration_declares_results_json_as_jsonb_not_text(self):
        columns = _migration_diagnostic_results_columns()

        assert columns["results_json"] == "JSONB"

    def test_migration_declares_run_at_as_timestamptz_not_text(self):
        columns = _migration_diagnostic_results_columns()

        assert columns["run_at"] == "TIMESTAMPTZ"


class TestConstructorDoesNotCreateTables:
    """Bug #1662 (mirrors Bug #1655's F4 remediation for wiki_cache_backend.py):
    the self-heal `CREATE TABLE IF NOT EXISTS` block in
    DiagnosticsPostgresBackend._ensure_schema() is dead code in production --
    `service_init.py` always runs MigrationRunner (fail-fast) before
    StorageFactory.create_backends() constructs any PostgreSQL backend in
    postgres storage mode, and DiagnosticsPostgresBackend is only ever
    constructed from that factory. Its only real effect was being a SECOND,
    silently-drifting source of truth for the schema (it declared both
    columns TEXT while the real migration declares JSONB/TIMESTAMPTZ). The
    dead block is removed entirely rather than re-synced, so there is no
    second copy left to drift again.
    """

    def test_constructor_issues_zero_create_table_statements(self):
        pool = _make_mock_pool()

        _make_backend(pool)

        sql_statements = _all_executed_sql(pool)
        create_table_statements = [s for s in sql_statements if "CREATE TABLE" in s]

        assert create_table_statements == []

    def test_constructor_does_not_call_conn_execute_at_all(self):
        """The whole point of removing the dead _ensure_schema() body is
        that construction no longer talks to the database at all."""
        pool = _make_mock_pool()

        _make_backend(pool)

        conn = _get_conn(pool)
        assert conn.execute.call_count == 0


class TestLoadResultsReturnNativeTypesVerbatim:
    """Bug #1662: load_all_results()/load_category_results() must return
    whatever psycopg hands back verbatim -- a native dict/list for the
    JSONB column and a native datetime for the TIMESTAMPTZ column -- never
    coerce/stringify them. This is the runtime behavior the widened
    `object`-typed Protocol return annotation exists to make honest;
    these tests lock that behavior in as a regression guard.
    """

    def test_load_all_results_preserves_native_dict_and_datetime(self):
        run_at = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
        results_obj = [{"name": "PG Tool", "status": "working"}]
        pool = _make_mock_pool(fetchall_return=[("cli_tools", results_obj, run_at)])
        backend = _make_backend(pool)

        rows = backend.load_all_results()

        assert rows == [("cli_tools", results_obj, run_at)]
        category, results_json, loaded_run_at = rows[0]
        assert isinstance(results_json, list), (
            "results_json must pass through as the native JSONB-deserialized "
            "object (a list here), never coerced to str"
        )
        assert isinstance(loaded_run_at, datetime), (
            "run_at must pass through as the native TIMESTAMPTZ-deserialized "
            "datetime, never coerced to str"
        )

    def test_load_category_results_preserves_native_dict_and_datetime(self):
        run_at = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)
        results_obj = {"name": "PG Tool", "status": "working"}
        pool = _make_mock_pool(fetchone_return=(results_obj, run_at))
        backend = _make_backend(pool)

        result = backend.load_category_results("cli_tools")

        assert result is not None
        results_json, loaded_run_at = result
        assert results_json is results_obj
        assert isinstance(loaded_run_at, datetime)

    def test_load_category_results_returns_none_when_absent(self):
        pool = _make_mock_pool(fetchone_return=None)
        backend = _make_backend(pool)

        result = backend.load_category_results("cli_tools")

        assert result is None


class TestSatisfiesDiagnosticsBackendProtocol:
    def test_isinstance_check_against_protocol(self):
        from code_indexer.server.storage.protocols import DiagnosticsBackend

        pool = _make_mock_pool()
        backend = _make_backend(pool)

        assert isinstance(backend, DiagnosticsBackend)


class TestDiagnosticsBackendProtocolReturnAnnotationsAreObjectTyped:
    """Bug #1662 regression guard (code review gap): `runtime_checkable`
    Protocol `isinstance()` checks only member PRESENCE, never method
    signatures/type annotations. Reverting `DiagnosticsBackend.load_all_results()`/
    `load_category_results()`'s return annotations back to the old, provably
    false `str`-typed contract (`List[Tuple[str, str, str]]` /
    `Optional[Tuple[str, str]]`) would leave `test_isinstance_check_against_protocol`
    above -- and every other test in this file -- green, silently reopening
    the exact bug this file exists to guard against. These tests introspect
    the Protocol method's `__annotations__` directly so that specific revert
    is caught.
    """

    def test_load_all_results_return_annotation_is_object_typed_not_str(self):
        from code_indexer.server.storage.protocols import DiagnosticsBackend

        annotation = DiagnosticsBackend.load_all_results.__annotations__["return"]

        assert "object" in annotation
        assert "Tuple[str, str, str]" not in annotation

    def test_load_category_results_return_annotation_is_object_typed_not_str(self):
        from code_indexer.server.storage.protocols import DiagnosticsBackend

        annotation = DiagnosticsBackend.load_category_results.__annotations__["return"]

        assert "object" in annotation
        assert "Tuple[str, str]" not in annotation
