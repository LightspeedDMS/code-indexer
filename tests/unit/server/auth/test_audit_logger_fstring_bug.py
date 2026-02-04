"""
Test suite for Bug #137 - Missing f-string prefixes in audit_logger.py.

This test suite verifies that audit logging methods properly interpolate
JSON data into log messages using f-strings, rather than outputting literal
'{json.dumps(log_entry)}' strings.

Foundation #1 Compliant: Uses real logging infrastructure with no mocks.
"""

import pytest
import json
import logging
from pathlib import Path
from unittest.mock import patch
import tempfile

from code_indexer.server.auth.audit_logger import PasswordChangeAuditLogger


class TestAuditLoggerFStringBug:
    """Test that audit logger methods properly interpolate f-strings."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test environment with real audit logging."""
        # Create temporary directory for audit logs
        self.temp_dir = Path(tempfile.mkdtemp())
        self.audit_log_path = self.temp_dir / "audit" / "test_audit.log"
        self.audit_log_path.parent.mkdir(exist_ok=True, parents=True)

        # Create audit logger instance with file path (not directory)
        self.audit_logger = PasswordChangeAuditLogger(str(self.audit_log_path))

        # Capture log records
        self.captured_logs = []

        # Create a handler that captures log records
        class ListHandler(logging.Handler):
            def __init__(self, log_list):
                super().__init__()
                self.log_list = log_list

            def emit(self, record):
                self.log_list.append(record)

        self.handler = ListHandler(self.captured_logs)

        # Get the logger used by audit_logger (it uses the root logger)
        logger = logging.getLogger("code_indexer.server.auth.audit_logger")
        logger.addHandler(self.handler)
        logger.setLevel(logging.DEBUG)

        yield

        # Cleanup
        logger.removeHandler(self.handler)
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _verify_log_contains_json(self, log_message: str, expected_fields: list):
        """
        Verify that log message contains actual JSON data, not literal string.

        Args:
            log_message: The log message to check
            expected_fields: List of field names that should appear in the JSON

        Raises:
            AssertionError: If log contains literal '{json.dumps(log_entry)}'
                          or if expected fields are not in the log
        """
        # CRITICAL: This should FAIL if f-string prefix is missing
        assert "{json.dumps(log_entry)}" not in log_message, \
            f"Log contains literal '{{json.dumps(log_entry)}}' - missing f-string prefix!"

        # Verify expected fields appear in the log message
        for field in expected_fields:
            assert field in log_message, \
                f"Expected field '{field}' not found in log message"

    def test_log_password_change_failure_interpolates_json(self):
        """Test that log_password_change_failure properly interpolates JSON (line 130)."""
        # Act
        self.audit_logger.log_password_change_failure(
            username="testuser",
            reason="Invalid old password",
            ip_address="192.168.1.1",
            additional_context={"attempt_count": 3}
        )

        # Assert
        assert len(self.captured_logs) > 0, "No log records captured"
        log_record = self.captured_logs[-1]
        log_message = log_record.getMessage()

        self._verify_log_contains_json(
            log_message,
            ["testuser", "Invalid old password", "192.168.1.1", "attempt_count"]
        )

    def test_log_rate_limit_triggered_interpolates_json(self):
        """Test that log_rate_limit_triggered properly interpolates JSON (line 163)."""
        # Act
        self.audit_logger.log_rate_limit_triggered(
            username="testuser",
            ip_address="192.168.1.1",
            attempt_count=5
        )

        # Assert
        assert len(self.captured_logs) > 0, "No log records captured"
        log_record = self.captured_logs[-1]
        log_message = log_record.getMessage()

        self._verify_log_contains_json(
            log_message,
            ["testuser", "192.168.1.1", "5"]
        )

    def test_log_concurrent_change_conflict_interpolates_json(self):
        """Test that log_concurrent_change_conflict properly interpolates JSON (line 190)."""
        # Act
        self.audit_logger.log_concurrent_change_conflict(
            username="testuser",
            ip_address="192.168.1.1"
        )

        # Assert
        assert len(self.captured_logs) > 0, "No log records captured"
        log_record = self.captured_logs[-1]
        log_message = log_record.getMessage()

        self._verify_log_contains_json(
            log_message,
            ["testuser", "192.168.1.1"]
        )

    def test_log_security_incident_interpolates_json(self):
        """Test that log_security_incident properly interpolates JSON (line 295)."""
        # Act
        self.audit_logger.log_security_incident(
            username="testuser",
            incident_type="unauthorized_access",
            ip_address="192.168.1.1",
            additional_context={"target_resource": "/admin"}
        )

        # Assert
        assert len(self.captured_logs) > 0, "No log records captured"
        log_record = self.captured_logs[-1]
        log_message = log_record.getMessage()

        self._verify_log_contains_json(
            log_message,
            ["testuser", "unauthorized_access", "192.168.1.1", "target_resource"]
        )

    def test_log_authentication_failure_interpolates_json(self):
        """Test that log_authentication_failure properly interpolates JSON (line 327)."""
        # Act
        self.audit_logger.log_authentication_failure(
            username="testuser",
            error_type="invalid_credentials",
            message="Invalid username or password",
            additional_context={"user_agent": "Mozilla/5.0"}
        )

        # Assert
        assert len(self.captured_logs) > 0, "No log records captured"
        log_record = self.captured_logs[-1]
        log_message = log_record.getMessage()

        self._verify_log_contains_json(
            log_message,
            ["testuser", "invalid_credentials", "Invalid username or password", "user_agent"]
        )

    def test_log_pr_creation_failure_interpolates_json(self):
        """Test that log_pr_creation_failure properly interpolates JSON (line 552)."""
        # Act
        self.audit_logger.log_pr_creation_failure(
            job_id="job_123",
            repo_alias="test-repo",
            reason="API rate limit exceeded",
            additional_context={"retry_after": 60}
        )

        # Assert
        assert len(self.captured_logs) > 0, "No log records captured"
        log_record = self.captured_logs[-1]
        log_message = log_record.getMessage()

        self._verify_log_contains_json(
            log_message,
            ["job_123", "test-repo", "API rate limit exceeded", "retry_after"]
        )

    def test_log_impersonation_denied_interpolates_json(self):
        """Test that log_impersonation_denied properly interpolates JSON (line 727)."""
        # Act
        self.audit_logger.log_impersonation_denied(
            actor_username="admin",
            target_username="targetuser",
            reason="Insufficient permissions",
            session_id="test_session_123",
            ip_address="192.168.1.1"
        )

        # Assert
        assert len(self.captured_logs) > 0, "No log records captured"
        log_record = self.captured_logs[-1]
        log_message = log_record.getMessage()

        self._verify_log_contains_json(
            log_message,
            ["admin", "targetuser", "Insufficient permissions", "192.168.1.1"]
        )
