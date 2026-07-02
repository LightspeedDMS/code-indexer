"""
Unit tests for Bug #835 Change 1 — lifecycle_schema_version in tracking backends.

Both SQLite and PostgreSQL DescriptionRefreshTrackingBackend must include
lifecycle_schema_version in the dicts returned by get_stale_repos() and
get_tracking_record(), so the scheduler can gate on it.

Tests:
4. test_sqlite_get_stale_repos_includes_lifecycle_schema_version
5. test_postgres_get_stale_repos_includes_lifecycle_schema_version
6. test_sqlite_get_tracking_record_includes_lifecycle_schema_version
7. test_postgres_get_tracking_record_includes_lifecycle_schema_version
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _make_sqlite_db(tmp_path: Path, alias: str, lifecycle_version: int) -> str:
    """Initialize real DB, seed a tracking row with given lifecycle_schema_version."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )

    db_file = str(tmp_path / "test.db")
    DatabaseSchema(db_file).initialize_database()

    now = datetime.now(timezone.utc).isoformat()
    DescriptionRefreshTrackingBackend(db_file).upsert_tracking(
        repo_alias=alias,
        status="pending",
        last_run="2020-01-01T00:00:00+00:00",
        next_run="2020-01-01T00:00:00+00:00",  # past — picked up as stale
        last_known_commit="abc123",
        created_at=now,
        updated_at=now,
    )
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            "UPDATE description_refresh_tracking SET lifecycle_schema_version=? WHERE repo_alias=?",
            (lifecycle_version, alias),
        )
    return db_file


# ---------------------------------------------------------------------------
# PostgreSQL pool mock helper
# ---------------------------------------------------------------------------


def _make_postgres_pool(row_tuple):
    """Return a mocked psycopg v3 connection pool that yields row_tuple from fetchall/fetchone."""
    cur = MagicMock()
    cur.fetchall.return_value = [row_tuple]
    cur.fetchone.return_value = row_tuple

    conn = MagicMock()
    conn.execute.return_value = cur

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    return pool


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSQLiteLifecycleVersionInBackend:
    """SQLite backend must expose lifecycle_schema_version in query results."""

    def test_sqlite_get_stale_repos_includes_lifecycle_schema_version(self, tmp_path):
        """get_stale_repos() must include lifecycle_schema_version with correct value."""
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
        )

        alias = "test-repo"
        expected_version = 3
        db_file = _make_sqlite_db(tmp_path, alias, lifecycle_version=expected_version)

        backend = DescriptionRefreshTrackingBackend(db_file)
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = backend.get_stale_repos(now_iso)

        assert len(rows) == 1
        assert "lifecycle_schema_version" in rows[0], (
            "get_stale_repos() must include lifecycle_schema_version in returned dict"
        )
        assert rows[0]["lifecycle_schema_version"] == expected_version

    def test_sqlite_get_tracking_record_includes_lifecycle_schema_version(
        self, tmp_path
    ):
        """get_tracking_record() must include lifecycle_schema_version with correct value."""
        from code_indexer.server.storage.sqlite_backends import (
            DescriptionRefreshTrackingBackend,
        )

        alias = "test-repo"
        expected_version = 5
        db_file = _make_sqlite_db(tmp_path, alias, lifecycle_version=expected_version)

        backend = DescriptionRefreshTrackingBackend(db_file)
        record = backend.get_tracking_record(alias)

        assert record is not None
        assert "lifecycle_schema_version" in record, (
            "get_tracking_record() must include lifecycle_schema_version in returned dict"
        )
        assert record["lifecycle_schema_version"] == expected_version


class TestPostgresLifecycleVersionInBackend:
    """PostgreSQL backend must expose lifecycle_schema_version in query results."""

    def test_postgres_get_stale_repos_includes_lifecycle_schema_version(self):
        """get_stale_repos() must include lifecycle_schema_version with correct value."""
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )

        expected_version = 2
        # Row order must match _SELECT_COLUMNS:
        # repo_alias, last_run, next_run, status, error,
        # last_known_commit, last_known_files_processed,
        # last_known_indexed_at, created_at, updated_at, lifecycle_schema_version
        row = (
            "test-repo",
            "2020-01-01",
            "2020-01-01",
            "pending",
            None,
            "abc123",
            10,
            "2020-01-01",
            "2020-01-01",
            "2020-01-01",
            expected_version,
        )
        pool = _make_postgres_pool(row)
        backend = DescriptionRefreshTrackingPostgresBackend(pool)

        rows = backend.get_stale_repos("2099-01-01T00:00:00+00:00")

        assert len(rows) == 1
        assert "lifecycle_schema_version" in rows[0], (
            "PostgreSQL get_stale_repos() must include lifecycle_schema_version"
        )
        assert rows[0]["lifecycle_schema_version"] == expected_version

    def test_postgres_get_tracking_record_includes_lifecycle_schema_version(self):
        """get_tracking_record() must include lifecycle_schema_version with correct value."""
        from code_indexer.server.storage.postgres.description_refresh_tracking_backend import (
            DescriptionRefreshTrackingPostgresBackend,
        )

        expected_version = 7
        row = (
            "test-repo",
            "2020-01-01",
            "2020-01-01",
            "pending",
            None,
            "abc123",
            10,
            "2020-01-01",
            "2020-01-01",
            "2020-01-01",
            expected_version,
        )
        pool = _make_postgres_pool(row)
        backend = DescriptionRefreshTrackingPostgresBackend(pool)

        record = backend.get_tracking_record("test-repo")

        assert record is not None
        assert "lifecycle_schema_version" in record, (
            "PostgreSQL get_tracking_record() must include lifecycle_schema_version"
        )
        assert record["lifecycle_schema_version"] == expected_version
