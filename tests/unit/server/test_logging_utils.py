"""
Unit tests for logging_utils module.

Tests the logging utility functions for formatting log messages with error codes.
"""


def test_format_error_log():
    """Test formatting an error log message with error code."""
    from code_indexer.server.logging_utils import format_error_log

    # Basic usage
    message = format_error_log(
        "AUTH-OIDC-001",
        "Failed to connect to OIDC provider",
        issuer="https://example.com",
    )
    assert message.startswith("[AUTH-OIDC-001]")
    assert "Failed to connect to OIDC provider" in message
    assert "issuer=https://example.com" in message

    # Without additional context
    message = format_error_log("MCP-TOOL-042", "Tool execution failed")
    assert message == "[MCP-TOOL-042] Tool execution failed"


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
        "normal_field": "visible",
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
