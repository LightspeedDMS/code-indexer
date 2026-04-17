"""
Tests for Story #728 Part 1A — lifecycle_schema_version column migration.

Covers:
1. SQLite fresh DB gets lifecycle_schema_version column with default 0
2. SQLite re-running initialize_database is idempotent (raw PRAGMA list checked for count=1)
3. SQLite existing rows receive default 0 after _migrate_description_refresh_lifecycle_version
4. SQLite migration method is a no-op when column already present (no raise)
5. PostgreSQL migration file: ALTER TABLE targeting description_refresh_tracking +
   "ADD COLUMN IF NOT EXISTS LIFECYCLE_SCHEMA_VERSION" combined fragment + DEFAULT 0
"""

import sqlite3
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

POSTGRES_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "storage"
    / "postgres"
    / "migrations"
    / "sql"
    / "019_lifecycle_schema_version.sql"
)


# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def fast_sqlite(monkeypatch):
    """Set CIDX_TEST_FAST_SQLITE=1 for the test duration; monkeypatch restores prior value."""
    monkeypatch.setenv("CIDX_TEST_FAST_SQLITE", "1")


def _get_column_names(db_path: Path) -> list:
    """Return raw list of column names from PRAGMA table_info for description_refresh_tracking.

    Returns a list (not a set/dict) so duplicate detection is possible.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute("PRAGMA table_info(description_refresh_tracking)")
        return [row[1] for row in cursor.fetchall()]
    finally:
        conn.close()


def _get_column_default(db_path: Path, column_name: str):
    """Return the dflt_value from PRAGMA table_info for the given column, or None if absent."""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute("PRAGMA table_info(description_refresh_tracking)")
        for row in cursor.fetchall():
            if row[1] == column_name:
                return row[4]  # dflt_value
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SQLite migration tests
# ---------------------------------------------------------------------------


class TestSQLiteMigration:
    def test_01_fresh_db_has_lifecycle_schema_version_column(
        self, tmp_path, fast_sqlite
    ):
        """Fresh DB initialized via DatabaseSchema gets lifecycle_schema_version column with default 0."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        DatabaseSchema(str(db_path)).initialize_database()

        names = _get_column_names(db_path)
        assert "lifecycle_schema_version" in names, (
            "lifecycle_schema_version column must exist after initialize_database"
        )
        default = _get_column_default(db_path, "lifecycle_schema_version")
        assert default == "0", f"Default must be '0', got {default!r}"

    def test_02_initialize_database_idempotent(self, tmp_path, fast_sqlite):
        """Re-running initialize_database does not raise and lifecycle_schema_version appears exactly once."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        mgr = DatabaseSchema(str(db_path))
        mgr.initialize_database()
        mgr.initialize_database()  # second call must not raise

        # Use raw list to detect duplicates — dict would mask them
        names = _get_column_names(db_path)
        count = names.count("lifecycle_schema_version")
        assert count == 1, (
            f"lifecycle_schema_version must appear exactly once, found {count}"
        )

    def test_03_existing_rows_default_to_zero_after_migration(
        self, tmp_path, fast_sqlite
    ):
        """Rows pre-dating the column receive lifecycle_schema_version = 0 after migration."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """CREATE TABLE description_refresh_tracking (
                    repo_alias TEXT PRIMARY KEY,
                    last_run TEXT, next_run TEXT,
                    status TEXT DEFAULT 'pending', error TEXT,
                    last_known_commit TEXT, last_known_files_processed INTEGER,
                    last_known_indexed_at TEXT, created_at TEXT, updated_at TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO description_refresh_tracking (repo_alias) VALUES ('test-repo')"
            )
            conn.commit()

            DatabaseSchema(str(db_path))._migrate_description_refresh_lifecycle_version(
                conn
            )
            conn.commit()

            row = conn.execute(
                "SELECT lifecycle_schema_version FROM description_refresh_tracking "
                "WHERE repo_alias = 'test-repo'"
            ).fetchone()
            assert row is not None
            assert row[0] == 0, f"Existing row must get default 0, got {row[0]!r}"
        finally:
            conn.close()

    def test_04_migration_method_no_op_when_column_present(self, tmp_path, fast_sqlite):
        """_migrate_description_refresh_lifecycle_version does not raise when column already exists."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """CREATE TABLE description_refresh_tracking (
                    repo_alias TEXT PRIMARY KEY,
                    lifecycle_schema_version INTEGER DEFAULT 0
                )"""
            )
            conn.commit()

            # Must not raise even when column already exists
            DatabaseSchema(str(db_path))._migrate_description_refresh_lifecycle_version(
                conn
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PostgreSQL migration file tests
# ---------------------------------------------------------------------------


class TestPostgreSQLMigrationFile:
    def test_05_postgres_migration_file_exists_with_correct_sql(self):
        """PostgreSQL migration 019_lifecycle_schema_version.sql exists with correct SQL.

        Asserts the file:
        - Is present on disk
        - Contains ALTER TABLE targeting description_refresh_tracking
        - Contains the combined fragment "ADD COLUMN IF NOT EXISTS LIFECYCLE_SCHEMA_VERSION"
          (ensures ADD COLUMN and the target column name appear together, not in separate clauses)
        - Sets DEFAULT 0
        """
        assert POSTGRES_MIGRATION_PATH.exists(), (
            f"PostgreSQL migration file not found: {POSTGRES_MIGRATION_PATH}"
        )

        sql = POSTGRES_MIGRATION_PATH.read_text().upper()
        assert "ALTER TABLE" in sql, "Migration must contain ALTER TABLE"
        assert "DESCRIPTION_REFRESH_TRACKING" in sql, (
            "Migration must target description_refresh_tracking table"
        )
        assert "ADD COLUMN IF NOT EXISTS LIFECYCLE_SCHEMA_VERSION" in sql, (
            "Migration must contain 'ADD COLUMN IF NOT EXISTS lifecycle_schema_version' as a combined clause"
        )
        assert "DEFAULT 0" in sql, "Migration must set DEFAULT 0"
