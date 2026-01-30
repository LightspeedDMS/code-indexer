"""
Unit tests for logging_utils module.

Tests the logging utility functions for formatting log messages with error codes.
"""

from unittest.mock import patch


def test_format_error_log():
    """Test formatting an error log message with error code."""
    from code_indexer.server.logging_utils import format_error_log

    # Basic usage
    message = format_error_log("AUTH-OIDC-001", "Failed to connect to OIDC provider", issuer="https://example.com")
    assert message.startswith("[AUTH-OIDC-001]")
    assert "Failed to connect to OIDC provider" in message
    assert "issuer=https://example.com" in message

    # Without additional context
    message = format_error_log("MCP-TOOL-042", "Tool execution failed")
    assert message == "[MCP-TOOL-042] Tool execution failed"


def test_format_with_correlation_id():
    """Test that correlation_id is included in extra dict when available."""
    from code_indexer.server.logging_utils import get_log_extra

    # With correlation_id
    with patch('code_indexer.server.logging_utils.get_correlation_id', return_value="test-corr-123"):
        extra = get_log_extra("AUTH-OIDC-001")
        assert extra == {"error_code": "AUTH-OIDC-001", "correlation_id": "test-corr-123"}

    # Without correlation_id
    with patch('code_indexer.server.logging_utils.get_correlation_id', return_value=None):
        extra = get_log_extra("AUTH-OIDC-002")
        assert extra == {"error_code": "AUTH-OIDC-002"}


def test_sanitize_sensitive_data():
    """Test that sensitive data like passwords and tokens are sanitized."""
    from code_indexer.server.logging_utils import sanitize_for_logging

    # Dictionary with sensitive keys
    data = {
        "username": "admin",
        "password": "secret123",
        "token": "abc123token",
        "api_key": "key123",
        "secret": "mysecret",
        "normal_field": "visible"
    }

    sanitized = sanitize_for_logging(data)

    # Sensitive fields should be masked
    assert sanitized["password"] == "***REDACTED***"
    assert sanitized["token"] == "***REDACTED***"
    assert sanitized["api_key"] == "***REDACTED***"
    assert sanitized["secret"] == "***REDACTED***"

    # Normal fields should be visible
    assert sanitized["username"] == "admin"
    assert sanitized["normal_field"] == "visible"

    # String input should be returned as-is
    assert sanitize_for_logging("plain string") == "plain string"

    # None should be handled
    assert sanitize_for_logging(None) is None
