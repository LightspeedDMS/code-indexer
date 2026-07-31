"""EVO-65021: workspace-mcp ``workspace_id`` retention on golden_repos_metadata.

Exercises the REAL GoldenRepoMetadataSqliteBackend (no mocked DB layer,
memory: feedback_faithful_db_mocks) for the new ``workspace_id`` column:

- add_repo persists workspace_id; get_repo / list_repos return it
- add_repo without workspace_id stores NULL (pre-retention repos)
- update_workspace_id backfills / clears it
- ensure_table_exists() migrates a LEGACY table (no workspace_id column)
  in place, so repos registered before this change keep working
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import shutil

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


@pytest.fixture
def temp_db():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    schema = DatabaseSchema(db_path)
    schema.initialize_database()
    yield db_path
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)


@pytest.fixture
def backend(temp_db):
    return GoldenRepoMetadataSqliteBackend(temp_db)


def _add(backend, alias, **overrides):
    kwargs = dict(
        alias=alias,
        repo_url="https://example.com/repo.git",
        default_branch="main",
        clone_path=f"/golden/{alias}",
        created_at="2026-01-01T00:00:00+00:00",
    )
    kwargs.update(overrides)
    backend.add_repo(**kwargs)


class TestWorkspaceIdPersistence:
    def test_add_repo_persists_workspace_id_and_get_repo_returns_it(self, backend):
        _add(backend, "repo-a", workspace_id="ws-deadbeef")
        row = backend.get_repo("repo-a")
        assert row is not None
        assert row["workspace_id"] == "ws-deadbeef"

    def test_add_repo_without_workspace_id_stores_none(self, backend):
        _add(backend, "repo-b")
        row = backend.get_repo("repo-b")
        assert row is not None
        assert row["workspace_id"] is None

    def test_list_repos_includes_workspace_id(self, backend):
        _add(backend, "repo-c", workspace_id="ws-cafef00d")
        _add(backend, "repo-d")
        by_alias = {r["alias"]: r for r in backend.list_repos()}
        assert by_alias["repo-c"]["workspace_id"] == "ws-cafef00d"
        assert by_alias["repo-d"]["workspace_id"] is None

    def test_list_repos_with_categories_includes_workspace_id(self, backend):
        """Backend parity: the categories listing carries workspace_id too, so
        it never silently omits the key the Postgres mirror returns."""
        _add(backend, "repo-g", workspace_id="ws-1a2b3c")
        _add(backend, "repo-h")
        by_alias = {r["alias"]: r for r in backend.list_repos_with_categories()}
        assert by_alias["repo-g"]["workspace_id"] == "ws-1a2b3c"
        assert by_alias["repo-h"]["workspace_id"] is None


def _count_top_level_columns(select_body: str) -> int:
    """Count comma-separated SELECT columns, ignoring commas inside parens
    (e.g. ``COALESCE(wiki_enabled, FALSE)`` is one column)."""
    depth = 0
    count = 1
    for ch in select_body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


class TestPostgresQueryColumnCount:
    """CRITICAL (EVO-65021): every Postgres query feeding ``_row_to_dict_full``
    (which reads ``row[10]``) MUST SELECT 11 columns, else the mapper raises
    ``IndexError`` on the first row. ``list_repos_with_categories`` regressed
    this. Static guard so it fails without a live PostgreSQL."""

    def _select_body(self, method) -> str:
        import inspect
        import re

        src = inspect.getsource(method)
        m = re.search(
            r"SELECT\s+(.*?)\s+FROM\s+golden_repos_metadata",
            src,
            re.IGNORECASE | re.DOTALL,
        )
        assert m is not None, f"no SELECT found in {method.__name__}"
        return m.group(1)

    def test_full_mapper_queries_select_eleven_columns(self):
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        for method in (
            GoldenRepoMetadataPostgresBackend.get_repo,
            GoldenRepoMetadataPostgresBackend.list_repos_with_categories,
        ):
            body = self._select_body(method)
            assert "workspace_id" in body, method.__name__
            assert _count_top_level_columns(body) == 11, (
                f"{method.__name__} must SELECT 11 columns to feed "
                f"_row_to_dict_full (row[10]); got {_count_top_level_columns(body)}"
            )

    def test_basic_mapper_query_selects_nine_columns(self):
        from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
            GoldenRepoMetadataPostgresBackend,
        )

        body = self._select_body(GoldenRepoMetadataPostgresBackend.list_repos)
        assert "workspace_id" in body
        assert _count_top_level_columns(body) == 9


class TestUpdateWorkspaceId:
    def test_update_workspace_id_backfills_existing_repo(self, backend):
        _add(backend, "repo-e")  # pre-retention: NULL
        assert backend.update_workspace_id("repo-e", "ws-11112222") is True
        assert backend.get_repo("repo-e")["workspace_id"] == "ws-11112222"

    def test_update_workspace_id_can_clear(self, backend):
        _add(backend, "repo-f", workspace_id="ws-33334444")
        assert backend.update_workspace_id("repo-f", None) is True
        assert backend.get_repo("repo-f")["workspace_id"] is None

    def test_update_workspace_id_missing_alias_returns_false(self, backend):
        assert backend.update_workspace_id("nope", "ws-55556666") is False


class TestLegacyTableMigration:
    def test_ensure_table_exists_adds_workspace_id_to_legacy_table(self):
        """A golden_repos_metadata table created WITHOUT workspace_id (a DB from
        before EVO-65021) gains the column when the backend initialises it."""
        temp_dir = tempfile.mkdtemp()
        try:
            db_path = os.path.join(temp_dir, "legacy.db")
            # Build a legacy-shaped table (no workspace_id column).
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE golden_repos_metadata (
                    alias TEXT PRIMARY KEY NOT NULL,
                    repo_url TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    clone_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    enable_temporal INTEGER NOT NULL DEFAULT 0,
                    temporal_options TEXT,
                    wiki_enabled INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                "INSERT INTO golden_repos_metadata "
                "(alias, repo_url, default_branch, clone_path, created_at) "
                "VALUES ('legacy', 'u', 'main', '/c', '2026-01-01T00:00:00+00:00')"
            )
            conn.commit()
            cols_before = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(golden_repos_metadata)"
                ).fetchall()
            }
            conn.close()
            assert "workspace_id" not in cols_before

            backend = GoldenRepoMetadataSqliteBackend(db_path)
            backend.ensure_table_exists()

            # Column is present and the legacy row reads back with NULL id...
            row = backend.get_repo("legacy")
            assert row is not None
            assert row["workspace_id"] is None
            # ...and can be backfilled.
            assert backend.update_workspace_id("legacy", "ws-77778888") is True
            assert backend.get_repo("legacy")["workspace_id"] == "ws-77778888"
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
