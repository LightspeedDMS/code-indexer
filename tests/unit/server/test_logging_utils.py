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


class TestMaskUrlCredentials:
    """mask_url_credentials strips embedded clone credentials before a repo URL
    is exposed in an API response."""

    def test_masks_oauth2_token(self):
        from code_indexer.server.logging_utils import mask_url_credentials

        masked = mask_url_credentials(
            "https://oauth2:glpat-SECRET123@gitlab.com/org/repo.git"
        )
        assert masked == "https://***@gitlab.com/org/repo.git"
        assert "glpat" not in masked
        assert "SECRET123" not in masked

    def test_masks_user_password(self):
        from code_indexer.server.logging_utils import mask_url_credentials

        assert (
            mask_url_credentials("https://user:pass@github.com/a/b.git")
            == "https://***@github.com/a/b.git"
        )

    def test_leaves_credential_free_url_unchanged(self):
        from code_indexer.server.logging_utils import mask_url_credentials

        for url in (
            "https://gitlab.com/org/repo.git",
            "local://myalias",
            "git@github.com:org/repo.git",  # scp-form, no scheme -> username, not a secret
        ):
            assert mask_url_credentials(url) == url

    def test_idempotent_and_non_string_safe(self):
        from code_indexer.server.logging_utils import mask_url_credentials

        once = mask_url_credentials("https://oauth2:tok@gitlab.com/x.git")
        assert mask_url_credentials(once) == once  # masking twice is a no-op
        assert mask_url_credentials(None) is None
        assert mask_url_credentials(123) == 123
