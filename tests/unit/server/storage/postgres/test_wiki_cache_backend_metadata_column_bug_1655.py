"""
Unit tests for Bug #1655: wiki_cache_backend.py references metadata_json but
the real migration declares the column as metadata.

Migration `storage/postgres/migrations/sql/001_initial_schema.sql` declares
`wiki_cache.metadata JSONB` (plain `metadata`, no `metadata_json` — no
rename migration exists anywhere in the migrations directory). But
WikiCachePostgresBackend's `_ensure_schema()`, `get_article()`, and
`put_article()` all reference a column named `metadata_json` instead.

Impact: get_article() raises psycopg.errors.UndefinedColumn on PostgreSQL —
breaking wiki article caching cluster-wide.

Tests mock ConnectionPool (no real PostgreSQL required — same pattern as
test_query_embedding_cache_backend_1105.py / test_xray_cache_backend.py:
pool.connection() -> conn context manager, conn.execute(sql, params)
returns a cursor-like mock). Assertions inspect the literal SQL text passed
to conn.execute() so they fail against the current buggy code (which emits
"metadata_json") and pass once the column reference is corrected to
"metadata". A migration-file cross-check pins the ground truth so a future
migration change that renames the column again would also be caught here.

The SQLite side (WikiCacheSqliteBackend in storage/sqlite_backends.py) is
verified separately: it manages its own inline schema (not the shared
PostgreSQL migration file) and is self-consistently named `metadata_json`
throughout — no fix needed there. A regression test below confirms this
so a future change cannot silently break SQLite while "fixing" PostgreSQL.
"""

from __future__ import annotations

import re
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


def _migration_wiki_cache_columns() -> set:
    """Parse the real migration file and return wiki_cache's column names.

    Ground truth for this bug: reads the actual on-disk migration SQL
    rather than a hardcoded expectation, so a future migration edit that
    changes the column set again is caught by this same test.
    """
    assert _MIGRATION_PATH.is_file(), f"migration file not found: {_MIGRATION_PATH}"
    sql = _MIGRATION_PATH.read_text()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS wiki_cache \((.*?)\n\);", sql, re.DOTALL
    )
    assert match is not None, "wiki_cache CREATE TABLE not found in migration"
    body = match.group(1)
    columns = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.upper().startswith(("PRIMARY KEY", "--")):
            continue
        columns.add(line.split()[0])
    return columns


# ---------------------------------------------------------------------------
# Mock pool helpers (identical pattern to test_query_embedding_cache_backend_1105.py)
# ---------------------------------------------------------------------------


def _make_mock_pool(fetchone_return: Any = None) -> MagicMock:
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    conn.execute.return_value = cursor
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


def _get_conn(pool: MagicMock) -> Any:
    return pool.connection.return_value.__enter__.return_value


def _make_backend(pool: MagicMock) -> Any:
    from code_indexer.server.storage.postgres.wiki_cache_backend import (
        WikiCachePostgresBackend,
    )

    backend = WikiCachePostgresBackend(pool)
    return backend


def _all_executed_sql(pool: MagicMock) -> list:
    conn = _get_conn(pool)
    return [call.args[0] for call in conn.execute.call_args_list]


class TestMigrationGroundTruth:
    """Pin the real migration's wiki_cache column set as ground truth."""

    def test_migration_declares_metadata_not_metadata_json(self):
        columns = _migration_wiki_cache_columns()

        assert "metadata" in columns
        assert "metadata_json" not in columns


class TestConstructorDoesNotCreateTables:
    """Code-review remediation (F4, on the #1652/#1655 commit): the
    self-heal `CREATE TABLE IF NOT EXISTS` block in `_ensure_schema()` was
    dead code in production — `service_init.py` always runs
    `MigrationRunner` before `StorageFactory.create_backends()` constructs
    any PostgreSQL backend (postgres storage mode), and `WikiCachePostgresBackend`
    is only ever constructed from that factory. Its only real effect was
    being a SECOND, silently-drifting source of truth for the schema — the
    exact mechanism that produced Bug #1655 (metadata vs metadata_json) and
    that also mis-declared wiki_cache.rendered_at, wiki_sidebar_cache.sidebar_json,
    and wiki_sidebar_cache.built_at as TEXT instead of the migration's
    TIMESTAMPTZ/JSONB types. The dead CREATE TABLE block is removed
    entirely rather than re-synced, so there is no second copy left to
    drift again."""

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


class TestGetArticleColumnName:
    def test_get_article_select_does_not_reference_metadata_json(self):
        """
        Bug #1655 core reproduction: on real PostgreSQL this SELECT raises
        psycopg.errors.UndefinedColumn because wiki_cache has no
        metadata_json column (only `metadata`).
        """
        pool = _make_mock_pool(fetchone_return=None)
        backend = _make_backend(pool)
        _get_conn(pool).reset_mock()

        backend.get_article("my-repo", "some/article")

        conn = _get_conn(pool)
        assert conn.execute.call_count == 1
        select_sql = conn.execute.call_args.args[0]
        assert "metadata_json" not in select_sql
        assert re.search(r"\bmetadata\b", select_sql) is not None

    def test_get_article_selected_columns_are_subset_of_migration_columns(self):
        """Cross-check every column named in the SELECT clause actually
        exists in the real migration's wiki_cache table."""
        pool = _make_mock_pool(fetchone_return=None)
        backend = _make_backend(pool)
        _get_conn(pool).reset_mock()

        backend.get_article("my-repo", "some/article")

        select_sql = _get_conn(pool).execute.call_args.args[0]
        select_clause = select_sql.split("FROM")[0]
        selected_columns = {
            c.strip() for c in select_clause.replace("SELECT", "").split(",")
        }
        migration_columns = _migration_wiki_cache_columns()
        assert selected_columns.issubset(migration_columns), (
            f"{selected_columns - migration_columns} not in migration schema"
        )

    def test_get_article_returns_row_keyed_metadata_json_for_caller_compat(self):
        """The Python-level dict key stays `metadata_json` (Protocol/caller
        contract, e.g. wiki_cache.py) even though the DB column is `metadata`."""
        pool = _make_mock_pool(
            fetchone_return=("<html>", "Title", 123.0, 456, {"author": "alice"})
        )
        backend = _make_backend(pool)

        result = backend.get_article("my-repo", "some/article")

        assert result is not None
        assert result["metadata_json"] == {"author": "alice"}


class TestPutArticleColumnName:
    def test_put_article_insert_does_not_reference_metadata_json(self):
        """
        Bug #1655: on real PostgreSQL this INSERT raises UndefinedColumn
        for the same reason as get_article's SELECT.
        """
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        _get_conn(pool).reset_mock()

        backend.put_article(
            "my-repo",
            "some/article",
            "<html>",
            "Title",
            123.0,
            456,
            "2026-08-24T00:00:00",
            '{"author": "alice"}',
        )

        conn = _get_conn(pool)
        insert_sql = conn.execute.call_args_list[0].args[0]
        assert "metadata_json" not in insert_sql
        assert re.search(r"\bmetadata\b", insert_sql) is not None

    def test_put_article_inserted_columns_are_subset_of_migration_columns(self):
        pool = _make_mock_pool()
        backend = _make_backend(pool)
        _get_conn(pool).reset_mock()

        backend.put_article(
            "my-repo",
            "some/article",
            "<html>",
            "Title",
            123.0,
            456,
            "2026-08-24T00:00:00",
            '{"author": "alice"}',
        )

        insert_sql = _get_conn(pool).execute.call_args_list[0].args[0]
        columns_match = re.search(
            r"INSERT INTO wiki_cache\s*\((.*?)\)", insert_sql, re.DOTALL
        )
        assert columns_match is not None
        inserted_columns = {c.strip() for c in columns_match.group(1).split(",")}
        migration_columns = _migration_wiki_cache_columns()
        assert inserted_columns.issubset(migration_columns), (
            f"{inserted_columns - migration_columns} not in migration schema"
        )


class TestSqliteBackendUnaffected:
    """Confirm WikiCacheSqliteBackend legitimately uses metadata_json — it
    manages its own inline schema (Story #289), not the shared PostgreSQL
    migration file, so no rename is needed there."""

    def test_sqlite_backend_get_article_uses_metadata_json_column(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import WikiCacheSqliteBackend

        backend = WikiCacheSqliteBackend(str(tmp_path / "t.db"))
        try:
            backend.put_article(
                "my-repo",
                "some/article",
                "<html>",
                "Title",
                123.0,
                456,
                "2026-08-24T00:00:00",
                '{"author": "alice"}',
            )
            result = backend.get_article("my-repo", "some/article")
        finally:
            backend.close()

        assert result is not None
        assert result["metadata_json"] == '{"author": "alice"}'
