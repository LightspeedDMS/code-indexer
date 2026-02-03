"""
Tests for self_monitoring database schema (Story #73 - AC5c).

Verifies that self_monitoring_issues table has all required columns
for three-tier deduplication algorithm.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseSchema


class TestSelfMonitoringSchema:
    """Test self_monitoring_issues table schema."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database with schema."""
        # Create temp directory to avoid chmod /tmp permission error
        temp_dir = tempfile.mkdtemp()
        db_path = Path(temp_dir) / "test.db"

        try:
            # Create schema
            schema = DatabaseSchema(db_path=str(db_path))
            schema.initialize_database()

            yield str(db_path)
        finally:
            db_path.unlink(missing_ok=True)
            Path(temp_dir).rmdir()

    def test_self_monitoring_issues_has_error_codes_column(self, temp_db):
        """Test that error_codes column exists for Tier 1 deduplication."""
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute("PRAGMA table_info(self_monitoring_issues)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            assert "error_codes" in columns, "error_codes column missing"
            assert columns["error_codes"] == "TEXT"
        finally:
            conn.close()

    def test_self_monitoring_issues_has_fingerprint_column(self, temp_db):
        """Test that fingerprint column exists for Tier 2 deduplication."""
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute("PRAGMA table_info(self_monitoring_issues)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            assert "fingerprint" in columns, "fingerprint column missing"
            assert columns["fingerprint"] == "TEXT"
        finally:
            conn.close()

    def test_self_monitoring_issues_has_source_files_column(self, temp_db):
        """Test that source_files column exists for fingerprint computation."""
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute("PRAGMA table_info(self_monitoring_issues)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            assert "source_files" in columns, "source_files column missing"
            assert columns["source_files"] == "TEXT"
        finally:
            conn.close()

    def test_can_insert_row_with_all_deduplication_fields(self, temp_db):
        """Test that rows can be inserted with all AC5c required fields."""
        conn = sqlite3.connect(temp_db)
        try:
            # First create a scan record (required by foreign key)
            conn.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end) "
                "VALUES (?, ?, ?, ?, ?)",
                ("scan-123", "2026-01-30T12:00:00", "SUCCESS", 1, 10),
            )

            # Insert issue with all deduplication fields
            conn.execute(
                "INSERT INTO self_monitoring_issues "
                "(scan_id, github_issue_number, github_issue_url, classification, "
                "title, error_codes, fingerprint, source_log_ids, source_files, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "scan-123",
                    101,
                    "https://github.com/org/repo/issues/101",
                    "server_bug",
                    "[BUG] Test issue",
                    "GIT-SYNC-001,AUTH-TOKEN-005",
                    "abc123def456",
                    "1,2,3",
                    "src/auth.py,src/git.py",
                    "2026-01-30T12:05:00",
                ),
            )
            conn.commit()

            # Verify data
            cursor = conn.execute(
                "SELECT error_codes, fingerprint, source_files "
                "FROM self_monitoring_issues WHERE github_issue_number = ?",
                (101,),
            )
            row = cursor.fetchone()

            assert row is not None
            assert row[0] == "GIT-SYNC-001,AUTH-TOKEN-005"
            assert row[1] == "abc123def456"
            assert row[2] == "src/auth.py,src/git.py"
        finally:
            conn.close()
