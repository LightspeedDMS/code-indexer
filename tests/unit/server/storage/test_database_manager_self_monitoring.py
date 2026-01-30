"""
Unit tests for self-monitoring SQLite tables (Story #72 - AC2).

Tests schema creation and structure for:
- self_monitoring_scans table
- self_monitoring_issues table
"""

import sqlite3
from pathlib import Path
import pytest
from code_indexer.server.storage.database_manager import DatabaseSchema


class TestSelfMonitoringSchema:
    """Test suite for self-monitoring database schema."""

    def test_self_monitoring_scans_table_created(self, tmp_path: Path) -> None:
        """Test self_monitoring_scans table is created by initialize_database()."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='self_monitoring_scans'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None
        assert result[0] == "self_monitoring_scans"

    def test_self_monitoring_scans_table_structure(self, tmp_path: Path) -> None:
        """Test self_monitoring_scans table has correct columns and types."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(self_monitoring_scans)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        expected_columns = {
            "scan_id": "TEXT",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "status": "TEXT",
            "log_id_start": "INTEGER",
            "log_id_end": "INTEGER",
            "issues_created": "INTEGER",
            "error_message": "TEXT",
        }
        assert columns == expected_columns

    def test_self_monitoring_scans_primary_key(self, tmp_path: Path) -> None:
        """Test self_monitoring_scans table has scan_id as primary key."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(self_monitoring_scans)")
        columns_info = cursor.fetchall()
        conn.close()

        # Find scan_id column and check if it's primary key (pk column = 1)
        scan_id_info = [col for col in columns_info if col[1] == "scan_id"][0]
        is_primary_key = scan_id_info[5]  # pk flag is at index 5
        assert is_primary_key == 1

    def test_self_monitoring_issues_table_created(self, tmp_path: Path) -> None:
        """Test self_monitoring_issues table is created by initialize_database()."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='self_monitoring_issues'"
        )
        result = cursor.fetchone()
        conn.close()

        assert result is not None
        assert result[0] == "self_monitoring_issues"

    def test_self_monitoring_issues_table_structure(self, tmp_path: Path) -> None:
        """Test self_monitoring_issues table has correct columns and types."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(self_monitoring_issues)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}
        conn.close()

        expected_columns = {
            "id": "INTEGER",
            "scan_id": "TEXT",
            "github_issue_number": "INTEGER",
            "github_issue_url": "TEXT",
            "classification": "TEXT",
            "title": "TEXT",
            "source_log_ids": "TEXT",
            "created_at": "TEXT",
        }
        assert columns == expected_columns

    def test_self_monitoring_issues_primary_key(self, tmp_path: Path) -> None:
        """Test self_monitoring_issues table has id as autoincrement primary key."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(self_monitoring_issues)")
        columns_info = cursor.fetchall()
        conn.close()

        # Find id column and check if it's primary key (pk column = 1)
        id_info = [col for col in columns_info if col[1] == "id"][0]
        is_primary_key = id_info[5]  # pk flag is at index 5
        assert is_primary_key == 1

    def test_self_monitoring_issues_foreign_key_to_scans(self, tmp_path: Path) -> None:
        """Test self_monitoring_issues.scan_id references self_monitoring_scans.scan_id."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))
        # Enable foreign keys to test the constraint
        conn.execute("PRAGMA foreign_keys = ON")

        # Insert a scan
        conn.execute(
            """
            INSERT INTO self_monitoring_scans
            (scan_id, started_at, completed_at, status, log_id_start, log_id_end, issues_created)
            VALUES ('scan-123', '2025-01-01T00:00:00Z', '2025-01-01T00:05:00Z', 'completed', 1, 100, 2)
            """
        )

        # Insert an issue referencing the scan - should succeed
        conn.execute(
            """
            INSERT INTO self_monitoring_issues
            (scan_id, github_issue_number, github_issue_url, classification, title, source_log_ids, created_at)
            VALUES ('scan-123', 1, 'https://github.com/org/repo/issues/1', 'error', 'Test Issue', '1,2,3', '2025-01-01T00:01:00Z')
            """
        )

        # Try to insert an issue with non-existent scan_id - should fail
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO self_monitoring_issues
                (scan_id, github_issue_number, github_issue_url, classification, title, source_log_ids, created_at)
                VALUES ('nonexistent-scan', 2, 'https://github.com/org/repo/issues/2', 'warning', 'Bad Issue', '4,5', '2025-01-01T00:02:00Z')
                """
            )

        conn.close()

    def test_self_monitoring_tables_indexes(self, tmp_path: Path) -> None:
        """Test appropriate indexes are created for self-monitoring tables."""
        db_path = tmp_path / "test.db"
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        conn = sqlite3.connect(str(db_path))

        # Check indexes on self_monitoring_scans
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='self_monitoring_scans'"
        )
        scans_indexes = [row[0] for row in cursor.fetchall()]

        # Check indexes on self_monitoring_issues
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='self_monitoring_issues'"
        )
        issues_indexes = [row[0] for row in cursor.fetchall()]

        conn.close()

        # Should have at least index on started_at for scans
        assert any("started_at" in idx.lower() for idx in scans_indexes)

        # Should have at least index on scan_id for issues (for foreign key lookups)
        assert any("scan_id" in idx.lower() for idx in issues_indexes)
