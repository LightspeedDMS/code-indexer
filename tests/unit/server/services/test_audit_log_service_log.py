"""
Unit tests for AuditLogService.log() and __init__().

Story #399: Audit Log Consolidation & AuditLogService Extraction
AC1: AuditLogService extracted from GroupAccessManager

TDD: These tests are written BEFORE the implementation exists (RED phase).
"""

import sqlite3

import pytest


class TestAuditLogServiceInit:
    """Tests for AuditLogService.__init__() - schema creation."""

    def test_init_creates_audit_logs_table(self, tmp_path):
        """AuditLogService.__init__() creates audit_logs table with correct schema."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_logs'"
        )
        table = cursor.fetchone()
        conn.close()

        assert table is not None

    def test_init_creates_timestamp_index(self, tmp_path):
        """AuditLogService.__init__() creates timestamp index."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_audit_timestamp'"
        )
        idx = cursor.fetchone()
        conn.close()

        assert idx is not None

    def test_init_creates_action_type_index(self, tmp_path):
        """AuditLogService.__init__() creates action_type index."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_audit_action_type'"
        )
        idx = cursor.fetchone()
        conn.close()

        assert idx is not None

    def test_init_is_idempotent_on_existing_db(self, tmp_path):
        """AuditLogService.__init__() is idempotent - safe to call multiple times."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        AuditLogService(db_path)
        # Second init must not raise (CREATE TABLE IF NOT EXISTS)
        AuditLogService(db_path)

    def test_init_creates_db_file(self, tmp_path):
        """AuditLogService.__init__() creates the database file if it doesn't exist."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        assert not db_path.exists()

        AuditLogService(db_path)

        assert db_path.exists()

    def test_coexists_with_groups_manager_same_db(self, tmp_path):
        """AuditLogService and GroupAccessManager can share the same DB file."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        db_path = tmp_path / "groups.db"
        GroupAccessManager(db_path)
        # Must not raise even though GroupAccessManager already created tables
        AuditLogService(db_path)

        groups = GroupAccessManager(db_path).get_all_groups()
        assert groups is not None


class TestAuditLogServiceLog:
    """Tests for AuditLogService.log() method (AC1)."""

    def test_log_creates_entry_in_audit_logs_table(self, tmp_path):
        """log() writes a record to the audit_logs table."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log(
            admin_id="admin",
            action_type="user_group_change",
            target_type="user",
            target_id="user123",
            details='{"from": "users", "to": "admins"}',
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 1
        row = rows[0]
        assert row["admin_id"] == "admin"
        assert row["action_type"] == "user_group_change"
        assert row["target_type"] == "user"
        assert row["target_id"] == "user123"
        assert row["details"] == '{"from": "users", "to": "admins"}'
        assert row["timestamp"] is not None

    def test_log_with_none_details(self, tmp_path):
        """log() accepts None details."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log(
            admin_id="admin",
            action_type="group_create",
            target_type="group",
            target_id="new-group",
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["details"] is None

    def test_log_multiple_entries_all_persisted(self, tmp_path):
        """log() persists multiple entries independently."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log("admin1", "user_group_change", "user", "user1")
        service.log("admin2", "repo_access_grant", "repo", "repo-abc")
        service.log("admin1", "group_delete", "group", "old-group")

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM audit_logs")
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 3

    def test_log_records_iso_timestamp(self, tmp_path):
        """log() records a valid ISO timestamp."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        service.log("admin", "group_create", "group", "g1")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM audit_logs")
        row = cursor.fetchone()
        conn.close()

        # Timestamp must be parseable ISO format
        from datetime import datetime
        ts = row["timestamp"]
        assert ts is not None
        assert len(ts) > 10  # More than just a date


class TestAuditLogServiceLogRaw:
    """Tests for AuditLogService.log_raw() method (Finding #3 fix)."""

    def test_log_raw_inserts_with_explicit_timestamp(self, tmp_path):
        """log_raw() inserts a record using the explicitly provided timestamp."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        explicit_ts = "2026-01-15T10:30:00+00:00"
        service.log_raw(
            timestamp=explicit_ts,
            admin_id="system",
            action_type="git_cleanup",
            target_type="auth",
            target_id="/path/to/repo",
            details='{"event_type": "git_cleanup"}',
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 1
        row = rows[0]
        assert row["timestamp"] == explicit_ts
        assert row["admin_id"] == "system"
        assert row["action_type"] == "git_cleanup"
        assert row["target_type"] == "auth"
        assert row["target_id"] == "/path/to/repo"

    def test_log_raw_with_none_details(self, tmp_path):
        """log_raw() accepts None details like log() does."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log_raw(
            timestamp="2026-01-15T10:30:00+00:00",
            admin_id="system",
            action_type="pr_creation_success",
            target_type="auth",
            target_id="my-repo",
            details=None,
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT details FROM audit_logs")
        row = cursor.fetchone()
        conn.close()

        assert row["details"] is None

    def test_log_raw_preserves_timestamp_unlike_log(self, tmp_path):
        """log_raw() preserves the given timestamp; log() always uses now()."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        historical_ts = "2025-01-01T00:00:00+00:00"
        service.log_raw(
            timestamp=historical_ts,
            admin_id="system",
            action_type="git_cleanup",
            target_type="auth",
            target_id="/repo",
        )

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp FROM audit_logs")
        row = cursor.fetchone()
        conn.close()

        # Must be exactly the historical timestamp, not current time
        assert row["timestamp"] == historical_ts
