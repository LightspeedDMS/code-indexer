"""
Tests for repository categories database schema and migrations.

Story #180: Repository Category CRUD and Management UI
Tests AC6 (persistence) and schema migration requirements.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema


class TestRepoCategoriesSchema:
    """Test repo_categories table creation."""

    def test_initialize_database_creates_repo_categories_table(self):
        """Test that initialize_database() creates repo_categories table (AC6)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            schema = DatabaseSchema(str(db_path))

            schema.initialize_database()

            # Verify table exists
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='repo_categories'"
                )
                result = cursor.fetchone()
                assert result is not None, "repo_categories table should exist"

                # Verify table structure
                cursor = conn.execute("PRAGMA table_info(repo_categories)")
                columns = {row[1]: row[2] for row in cursor.fetchall()}

                assert "id" in columns
                assert "name" in columns
                assert "pattern" in columns
                assert "priority" in columns
                assert "created_at" in columns
                assert "updated_at" in columns

                # Verify unique constraint on name
                cursor = conn.execute("SELECT sql FROM sqlite_master WHERE name='repo_categories'")
                sql = cursor.fetchone()[0]
                assert "UNIQUE" in sql or "PRIMARY KEY" in sql

            finally:
                conn.close()

    def test_repo_categories_table_has_correct_schema(self):
        """Test that repo_categories table has all required columns with correct types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            schema = DatabaseSchema(str(db_path))

            schema.initialize_database()

            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute("PRAGMA table_info(repo_categories)")
                columns = [(row[1], row[2], row[3]) for row in cursor.fetchall()]  # name, type, notnull

                # Convert to dict for easier checking
                col_dict = {col[0]: {"type": col[1], "notnull": col[2]} for col in columns}

                # Verify column constraints
                assert col_dict["name"]["notnull"] == 1, "name should be NOT NULL"
                assert col_dict["pattern"]["notnull"] == 1, "pattern should be NOT NULL"
                assert col_dict["priority"]["notnull"] == 1, "priority should be NOT NULL"

            finally:
                conn.close()


class TestGoldenReposMetadataMigration:
    """Test migration adding category_id and category_auto_assigned to golden_repos_metadata."""

    def test_migration_adds_category_id_column_if_missing(self):
        """Test that migration adds category_id column to golden_repos_metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Create database with old schema (without category_id)
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS golden_repos_metadata (
                        alias TEXT PRIMARY KEY NOT NULL,
                        repo_url TEXT NOT NULL,
                        default_branch TEXT NOT NULL,
                        clone_path TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.commit()
            finally:
                conn.close()

            # Run migration via initialize_database
            schema = DatabaseSchema(str(db_path))
            schema.initialize_database()

            # Verify category_id column was added
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute("PRAGMA table_info(golden_repos_metadata)")
                columns = {row[1] for row in cursor.fetchall()}
                assert "category_id" in columns, "category_id column should be added by migration"
            finally:
                conn.close()

    def test_migration_adds_category_auto_assigned_column_if_missing(self):
        """Test that migration adds category_auto_assigned column to golden_repos_metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"

            # Create database with old schema
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS golden_repos_metadata (
                        alias TEXT PRIMARY KEY NOT NULL,
                        repo_url TEXT NOT NULL,
                        default_branch TEXT NOT NULL,
                        clone_path TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.commit()
            finally:
                conn.close()

            # Run migration
            schema = DatabaseSchema(str(db_path))
            schema.initialize_database()

            # Verify category_auto_assigned column was added
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute("PRAGMA table_info(golden_repos_metadata)")
                columns = {row[1] for row in cursor.fetchall()}
                assert "category_auto_assigned" in columns, "category_auto_assigned should be added"
            finally:
                conn.close()

    def test_migration_is_idempotent(self):
        """Test that migration can be run multiple times safely."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            schema = DatabaseSchema(str(db_path))

            # Run migration first time
            schema.initialize_database()

            # Insert test data
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute(
                    """INSERT INTO golden_repos_metadata
                       (alias, repo_url, default_branch, clone_path, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("test-repo", "https://github.com/test/repo", "main", "/tmp/test", "2024-01-01T00:00:00Z")
                )
                conn.commit()
            finally:
                conn.close()

            # Run migration second time - should not fail
            schema2 = DatabaseSchema(str(db_path))
            schema2.initialize_database()

            # Verify data is still there
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute("SELECT alias FROM golden_repos_metadata WHERE alias = ?", ("test-repo",))
                result = cursor.fetchone()
                assert result is not None, "Data should survive re-running migration"
            finally:
                conn.close()

    def test_category_id_has_foreign_key_on_delete_set_null(self):
        """Test that category_id has ON DELETE SET NULL constraint (AC4)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            schema = DatabaseSchema(str(db_path))
            schema.initialize_database()

            # Check foreign key constraint
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("PRAGMA foreign_keys = ON")

                # Get foreign key info
                cursor = conn.execute("PRAGMA foreign_key_list(golden_repos_metadata)")
                fks = cursor.fetchall()

                # Look for category_id foreign key
                category_fk = None
                for fk in fks:
                    # fk format: (id, seq, table, from, to, on_update, on_delete, match)
                    if fk[3] == "category_id":  # from column
                        category_fk = fk
                        break

                assert category_fk is not None, "category_id should have foreign key constraint"
                assert category_fk[2] == "repo_categories", "FK should reference repo_categories"
                assert category_fk[6] == "SET NULL", "ON DELETE should be SET NULL"

            finally:
                conn.close()
