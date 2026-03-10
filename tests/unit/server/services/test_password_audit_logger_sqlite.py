"""
Unit tests for PasswordChangeAuditLogger writing to SQLite via AuditLogService.

Story #399: Audit Log Consolidation & AuditLogService Extraction
AC2: PasswordChangeAuditLogger writes to audit_logs table

Tests verify:
- All major log methods write to audit_logs with correct field mapping
- admin_id=actor, action_type=event_type, target_type="auth", target_id=subject
- details field contains full JSON event data
- No file handler created when audit_service is provided
- Event type values preserved as action_type values

TDD: These tests are written BEFORE the implementation exists (RED phase).
"""

import json

import pytest


class TestPasswordChangeAuditLoggerSQLiteInit:
    """Tests for PasswordChangeAuditLogger initialization with AuditLogService."""

    def test_no_file_handler_created_when_audit_service_provided(self, tmp_path):
        """PasswordChangeAuditLogger does not create a file handler when audit_service is given."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        # No file audit_logger attribute should exist (or must be None)
        file_logger = getattr(logger, "audit_logger", None)
        assert file_logger is None

    def test_no_log_file_created_when_audit_service_provided(self, tmp_path):
        """PasswordChangeAuditLogger does not create password_audit.log file when using audit_service."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        # Log something - no file should be created
        logger.log_password_change_success(username="u", ip_address="1.2.3.4")

        # No .log files should exist in tmp_path
        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 0


class TestPasswordChangeSuccessMapping:
    """Tests for log_password_change_success() SQLite field mapping."""

    def test_writes_to_audit_logs_table(self, tmp_path):
        """log_password_change_success() writes to audit_logs."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_password_change_success(username="testuser", ip_address="127.0.0.1")

        logs, total = audit_service.query()

        assert total == 1
        log = logs[0]
        assert log["action_type"] == "password_change_success"
        assert log["target_type"] == "auth"
        assert log["admin_id"] == "testuser"
        assert log["target_id"] == "testuser"

    def test_details_contains_ip_address(self, tmp_path):
        """log_password_change_success() stores ip_address in details JSON."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_password_change_success(
            username="alice", ip_address="10.0.0.1", user_agent="Mozilla/5.0"
        )

        logs, _ = audit_service.query()
        details = json.loads(logs[0]["details"])

        assert details["ip_address"] == "10.0.0.1"
        assert details["user_agent"] == "Mozilla/5.0"
        assert "timestamp" in details


class TestAuthenticationFailureMapping:
    """Tests for log_authentication_failure() SQLite field mapping."""

    def test_writes_with_correct_action_type(self, tmp_path):
        """log_authentication_failure() writes action_type=authentication_failure."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_authentication_failure(
            username="baduser", error_type="invalid_password", message="Wrong password"
        )

        logs, total = audit_service.query()

        assert total == 1
        assert logs[0]["action_type"] == "authentication_failure"
        assert logs[0]["target_type"] == "auth"
        assert logs[0]["admin_id"] == "baduser"


class TestImpersonationMapping:
    """Tests for impersonation log methods SQLite field mapping."""

    def test_log_impersonation_set_actor_as_admin_id(self, tmp_path):
        """log_impersonation_set() maps actor_username to admin_id, target to target_id."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_impersonation_set(
            actor_username="admin1",
            target_username="user1",
            session_id="sess-123",
            ip_address="192.168.1.1",
        )

        logs, total = audit_service.query()

        assert total == 1
        log = logs[0]
        assert log["action_type"] == "impersonation_set"
        assert log["admin_id"] == "admin1"
        assert log["target_type"] == "auth"
        assert log["target_id"] == "user1"

    def test_log_impersonation_cleared_writes_correctly(self, tmp_path):
        """log_impersonation_cleared() maps fields correctly."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_impersonation_cleared(
            actor_username="admin1",
            previous_target="user1",
            session_id="sess-123",
            ip_address="192.168.1.1",
        )

        logs, total = audit_service.query()

        assert total == 1
        assert logs[0]["action_type"] == "impersonation_cleared"
        assert logs[0]["admin_id"] == "admin1"

    def test_log_impersonation_denied_writes_correctly(self, tmp_path):
        """log_impersonation_denied() maps fields correctly."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_impersonation_denied(
            actor_username="regularuser",
            target_username="otheruser",
            reason="Not admin",
            session_id="sess-456",
            ip_address="10.0.0.2",
        )

        logs, total = audit_service.query()

        assert total == 1
        assert logs[0]["action_type"] == "impersonation_denied"
        assert logs[0]["admin_id"] == "regularuser"


class TestPRAndCleanupMapping:
    """Tests for PR creation and git cleanup log methods SQLite field mapping."""

    def test_log_pr_creation_success_uses_system_as_admin_id(self, tmp_path):
        """log_pr_creation_success() uses 'system' as admin_id, repo_alias as target_id."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_pr_creation_success(
            job_id="job-1",
            repo_alias="my-repo",
            branch_name="fix-branch",
            pr_url="https://github.com/owner/repo/pull/42",
            commit_hash="deadbeef",
            files_modified=["src/main.py"],
        )

        logs, total = audit_service.query()

        assert total == 1
        log = logs[0]
        assert log["action_type"] == "pr_creation_success"
        assert log["target_type"] == "auth"
        assert log["target_id"] == "my-repo"
        assert log["admin_id"] == "system"

    def test_log_pr_creation_failure_writes_to_sqlite(self, tmp_path):
        """log_pr_creation_failure() writes to audit_logs."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_pr_creation_failure(
            job_id="job-2",
            repo_alias="fail-repo",
            reason="push rejected",
        )

        logs, total = audit_service.query()

        assert total == 1
        assert logs[0]["action_type"] == "pr_creation_failure"
        assert logs[0]["target_id"] == "fail-repo"

    def test_log_pr_creation_disabled_writes_to_sqlite(self, tmp_path):
        """log_pr_creation_disabled() writes to audit_logs."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_pr_creation_disabled(job_id="job-3", repo_alias="disabled-repo")

        logs, total = audit_service.query()

        assert total == 1
        assert logs[0]["action_type"] == "pr_creation_disabled"

    def test_log_cleanup_uses_system_as_admin_id(self, tmp_path):
        """log_cleanup() uses 'system' as admin_id, repo_path as target_id."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_cleanup(
            repo_path="/path/to/repo",
            files_cleared=["file1.py", "file2.py"],
        )

        logs, total = audit_service.query()

        assert total == 1
        log = logs[0]
        assert log["action_type"] == "git_cleanup"
        assert log["target_type"] == "auth"
        assert log["target_id"] == "/path/to/repo"
        assert log["admin_id"] == "system"


class TestOAuthEventMapping:
    """Tests for OAuth event log methods SQLite field mapping."""

    def test_log_oauth_client_registration_writes_to_sqlite(self, tmp_path):
        """log_oauth_client_registration() writes to audit_logs."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_oauth_client_registration(
            client_id="client-123",
            client_name="My App",
            ip_address="1.2.3.4",
        )

        logs, total = audit_service.query()

        assert total == 1
        assert logs[0]["action_type"] == "oauth_client_registration"
        assert logs[0]["target_type"] == "auth"

    def test_log_token_refresh_success_writes_to_sqlite(self, tmp_path):
        """log_token_refresh_success() writes to audit_logs."""
        from code_indexer.server.services.audit_log_service import AuditLogService
        from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger

        db_path = tmp_path / "groups.db"
        audit_service = AuditLogService(db_path)
        logger = PasswordChangeAuditLogger(audit_service=audit_service)

        logger.log_token_refresh_success(
            username="myuser",
            ip_address="1.2.3.4",
            family_id="family-abc",
        )

        logs, total = audit_service.query()

        assert total == 1
        assert logs[0]["action_type"] == "token_refresh_success"
        assert logs[0]["admin_id"] == "myuser"
