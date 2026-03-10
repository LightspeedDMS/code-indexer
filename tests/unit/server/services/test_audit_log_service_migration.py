"""
Unit tests for migrate_flat_file_to_sqlite() function.

Story #399: Audit Log Consolidation & AuditLogService Extraction
AC4: Flat file migration on startup

Tests verify:
- Valid log line parsing with correct field mapping
- Malformed line skipping with count tracking
- Idempotency when no file exists
- File deletion after successful migration
- Mixed valid/invalid line handling

TDD: These tests are written BEFORE the implementation exists (RED phase).
"""

import json
import sqlite3

import pytest


class TestMigrateFlat:
    """Tests for migrate_flat_file_to_sqlite() function (AC4)."""

    def test_migration_is_idempotent_no_file(self, tmp_path):
        """migrate_flat_file_to_sqlite() silently skips when no file exists."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_file = tmp_path / "password_audit.log"
        # File does NOT exist

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 0
        assert skipped == 0

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM audit_logs")
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 0

    def test_migration_deletes_file_after_successful_migration(self, tmp_path):
        """migrate_flat_file_to_sqlite() deletes the log file on success."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_entry = {
            "event_type": "git_cleanup",
            "repo_path": "/some/repo",
            "files_cleared": [],
            "timestamp": "2026-03-01T10:00:00+00:00",
            "additional_context": {},
        }

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            f"2026-03-01 10:00:00 UTC - INFO - GIT_CLEANUP: {json.dumps(log_entry)}\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrate_flat_file_to_sqlite(log_file, service)

        assert not log_file.exists()

    def test_migration_deletes_file_even_with_all_malformed_lines(self, tmp_path):
        """migrate_flat_file_to_sqlite() deletes file even when all lines are malformed."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_file = tmp_path / "password_audit.log"
        log_file.write_text("not valid\nalso not valid\n")

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 0
        assert skipped == 2
        assert not log_file.exists()

    def test_migration_parses_git_cleanup_line(self, tmp_path):
        """Migration correctly parses a GIT_CLEANUP log line."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_entry = {
            "event_type": "git_cleanup",
            "repo_path": "/path/to/repo",
            "files_cleared": ["file1.py"],
            "timestamp": "2026-03-01T10:00:00+00:00",
            "additional_context": {},
        }

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            f"2026-03-01 10:00:00 UTC - INFO - GIT_CLEANUP: {json.dumps(log_entry)}\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 1
        assert skipped == 0

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["action_type"] == "git_cleanup"
        assert rows[0]["target_type"] == "auth"
        assert rows[0]["target_id"] == "/path/to/repo"

    def test_migration_parses_pr_creation_success_line(self, tmp_path):
        """Migration correctly parses a PR_CREATION_SUCCESS log line."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_entry = {
            "event_type": "pr_creation_success",
            "job_id": "scip-fix-123",
            "repo_alias": "my-repo",
            "branch_name": "scip-fix-branch",
            "pr_url": "https://github.com/owner/repo/pull/1",
            "commit_hash": "abc123",
            "files_modified": [],
            "timestamp": "2026-03-01T10:00:00+00:00",
            "additional_context": {},
        }

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            f"2026-03-01 10:00:00 UTC - INFO - PR_CREATION_SUCCESS: {json.dumps(log_entry)}\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 1
        assert skipped == 0

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["action_type"] == "pr_creation_success"
        assert rows[0]["target_type"] == "auth"
        assert rows[0]["target_id"] == "my-repo"

    def test_migration_parses_password_change_success_line(self, tmp_path):
        """Migration maps username to admin_id and target_id for auth events."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_entry = {
            "event_type": "password_change_success",
            "username": "testuser",
            "ip_address": "127.0.0.1",
            "timestamp": "2026-03-01T10:00:00+00:00",
            "user_agent": None,
            "additional_context": {},
        }

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            f"2026-03-01 10:00:00 UTC - INFO - PASSWORD_CHANGE_SUCCESS: {json.dumps(log_entry)}\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 1
        assert skipped == 0

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0]["action_type"] == "password_change_success"
        assert rows[0]["admin_id"] == "testuser"
        assert rows[0]["target_type"] == "auth"
        assert rows[0]["target_id"] == "testuser"

    def test_migration_skips_lines_without_json(self, tmp_path):
        """Migration skips lines without any JSON content."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            "This line has no JSON at all\n"
            "Another plain text line\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 0
        assert skipped == 2

    def test_migration_skips_lines_with_invalid_json(self, tmp_path):
        """Migration skips lines with malformed JSON."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            "2026-03-01 10:00:00 UTC - INFO - SOME_EVENT: not-valid-json\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 0
        assert skipped == 1

    def test_migration_handles_mixed_valid_and_invalid_lines(self, tmp_path):
        """Migration processes all valid lines even if some are malformed."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        entry1 = {
            "event_type": "git_cleanup",
            "repo_path": "/r1",
            "files_cleared": [],
            "timestamp": "2026-03-01T10:00:00+00:00",
            "additional_context": {},
        }
        entry2 = {
            "event_type": "pr_creation_success",
            "job_id": "j1",
            "repo_alias": "r1",
            "timestamp": "2026-03-01T10:00:01+00:00",
            "additional_context": {},
        }

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            f"2026-03-01 10:00:00 UTC - INFO - GIT_CLEANUP: {json.dumps(entry1)}\n"
            "corrupted line without JSON\n"
            f"2026-03-01 10:00:01 UTC - INFO - PR_CREATION_SUCCESS: {json.dumps(entry2)}\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrated, skipped = migrate_flat_file_to_sqlite(log_file, service)

        assert migrated == 2
        assert skipped == 1

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM audit_logs")
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 2

    def test_migration_stores_full_json_in_details(self, tmp_path):
        """Migration stores the full parsed JSON dict as details."""
        from code_indexer.server.services.audit_log_service import (
            AuditLogService,
            migrate_flat_file_to_sqlite,
        )

        log_entry = {
            "event_type": "git_cleanup",
            "repo_path": "/path/to/repo",
            "files_cleared": ["a.py", "b.py"],
            "timestamp": "2026-03-01T10:00:00+00:00",
            "additional_context": {"reason": "stale"},
        }

        log_file = tmp_path / "password_audit.log"
        log_file.write_text(
            f"2026-03-01 10:00:00 UTC - INFO - GIT_CLEANUP: {json.dumps(log_entry)}\n"
        )

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        migrate_flat_file_to_sqlite(log_file, service)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT details FROM audit_logs")
        row = cursor.fetchone()
        conn.close()

        assert row["details"] is not None
        details = json.loads(row["details"])
        assert details["event_type"] == "git_cleanup"
        assert details["files_cleared"] == ["a.py", "b.py"]
