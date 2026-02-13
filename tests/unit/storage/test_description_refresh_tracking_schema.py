"""
Unit tests for description_refresh_tracking table schema (Story #190 AC5).

Tests that DatabaseSchema creates the tracking table with correct fields.
"""

import sqlite3
import tempfile
from pathlib import Path
from code_indexer.server.storage.database_manager import DatabaseSchema


class TestDescriptionRefreshTrackingSchema:
    """Test description_refresh_tracking table creation and schema."""

    def test_initialize_database_creates_tracking_table(self):
        """AC5: DatabaseSchema.initialize_database() creates description_refresh_tracking table."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            schema = DatabaseSchema(db_path)

            schema.initialize_database()

            # Verify table exists
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='description_refresh_tracking'"
            )
            assert cursor.fetchone() is not None
            conn.close()

    def test_tracking_table_has_correct_columns(self):
        """AC5: Table has all required columns with correct types."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            schema = DatabaseSchema(db_path)
            schema.initialize_database()

            conn = sqlite3.connect(db_path)
            cursor = conn.execute("PRAGMA table_info(description_refresh_tracking)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}
            conn.close()

            # Verify all required columns exist
            assert "repo_alias" in columns
            assert "last_run" in columns
            assert "next_run" in columns
            assert "status" in columns
            assert "error" in columns
            assert "last_known_commit" in columns
            assert "last_known_files_processed" in columns
            assert "last_known_indexed_at" in columns
            assert "created_at" in columns
            assert "updated_at" in columns

            # Verify column types
            assert columns["repo_alias"] == "TEXT"
            assert columns["last_run"] == "TEXT"
            assert columns["next_run"] == "TEXT"
            assert columns["status"] == "TEXT"
            assert columns["error"] == "TEXT"
            assert columns["last_known_commit"] == "TEXT"
            assert columns["last_known_files_processed"] == "INTEGER"
            assert columns["last_known_indexed_at"] == "TEXT"
            assert columns["created_at"] == "TEXT"
            assert columns["updated_at"] == "TEXT"

    def test_repo_alias_is_primary_key(self):
        """AC5: repo_alias is the primary key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            schema = DatabaseSchema(db_path)
            schema.initialize_database()

            conn = sqlite3.connect(db_path)
            cursor = conn.execute("PRAGMA table_info(description_refresh_tracking)")
            columns = cursor.fetchall()
            conn.close()

            # Find repo_alias column
            repo_alias_col = next((col for col in columns if col[1] == "repo_alias"), None)
            assert repo_alias_col is not None
            # Column index 5 is the pk flag (1 if primary key, 0 otherwise)
            assert repo_alias_col[5] == 1

    def test_status_has_default_value_pending(self):
        """AC5: status column defaults to 'pending'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            schema = DatabaseSchema(db_path)
            schema.initialize_database()

            conn = sqlite3.connect(db_path)
            # Insert row with only repo_alias (required field)
            conn.execute(
                "INSERT INTO description_refresh_tracking (repo_alias) VALUES (?)",
                ("test-repo",)
            )
            conn.commit()

            # Verify status defaults to 'pending'
            cursor = conn.execute(
                "SELECT status FROM description_refresh_tracking WHERE repo_alias = ?",
                ("test-repo",)
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == "pending"
            conn.close()

    def test_table_creation_is_idempotent(self):
        """AC5: Calling initialize_database() multiple times is safe (CREATE TABLE IF NOT EXISTS)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            schema = DatabaseSchema(db_path)

            # Initialize once
            schema.initialize_database()

            # Initialize again - should not raise error
            schema.initialize_database()

            # Verify table still exists
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='description_refresh_tracking'"
            )
            assert cursor.fetchone() is not None
            conn.close()
