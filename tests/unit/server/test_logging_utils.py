"""
Unit tests for logging_utils module.

Tests the logging utility functions for formatting log messages with error codes.
"""

import pytest


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


def _make_log_record(msg: str = "test message"):
    import logging

    return logging.LogRecord(
        name="test.logging_utils.inject",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


@pytest.fixture
def _reset_correlation_contextvar_util():
    from code_indexer.server.telemetry.correlation_bridge import _correlation_id_var

    token = _correlation_id_var.set(None)
    try:
        yield
    finally:
        _correlation_id_var.reset(token)


class TestInjectCorrelationId:
    """Bug #1641: inject_correlation_id() is the shared helper both
    async_logging.IdentityQueueHandler.prepare() and
    SQLiteLogHandler.emit() call to heal the log store's correlation_id
    column for plain logger.x() calls that pass no extra=... at all."""

    def test_sets_correlation_id_from_active_context(
        self, _reset_correlation_contextvar_util
    ):
        from code_indexer.server.logging_utils import inject_correlation_id
        from code_indexer.server.telemetry.correlation_bridge import (
            set_current_correlation_id,
        )

        set_current_correlation_id("ctx-id-1641")
        record = _make_log_record()

        inject_correlation_id(record)

        assert record.correlation_id == "ctx-id-1641"

    def test_does_not_override_existing_correlation_id(
        self, _reset_correlation_contextvar_util
    ):
        from code_indexer.server.logging_utils import inject_correlation_id
        from code_indexer.server.telemetry.correlation_bridge import (
            set_current_correlation_id,
        )

        set_current_correlation_id("ambient-id")
        record = _make_log_record()
        record.correlation_id = "explicit-id"

        inject_correlation_id(record)

        assert record.correlation_id == "explicit-id"

    def test_no_op_when_no_active_context(self, _reset_correlation_contextvar_util):
        from code_indexer.server.logging_utils import inject_correlation_id

        record = _make_log_record()

        inject_correlation_id(record)

        assert getattr(record, "correlation_id", None) is None


class TestInjectTraceContext:
    """Story #1676 AC2: inject_trace_context() is the shared helper that
    async_logging.IdentityQueueHandler.prepare() and SQLiteLogHandler.emit()
    call to populate the log store's trace_id/span_id columns from the
    currently active OTEL span, mirroring inject_correlation_id()'s pattern
    exactly."""

    def test_sets_zero_values_when_no_active_span(self):
        from code_indexer.server.logging_utils import inject_trace_context

        record = _make_log_record()

        inject_trace_context(record)

        assert record.trace_id == "0" * 32
        assert record.span_id == "0" * 16

    def test_does_not_override_existing_trace_context(self):
        from code_indexer.server.logging_utils import inject_trace_context

        record = _make_log_record()
        record.trace_id = "explicit-trace-id"
        record.span_id = "explicit-span-id"

        inject_trace_context(record)

        assert record.trace_id == "explicit-trace-id"
        assert record.span_id == "explicit-span-id"

    def test_sets_real_ids_from_active_span(self):
        from code_indexer.server.logging_utils import inject_trace_context
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )
        from code_indexer.server.telemetry.spans import create_span, reset_spans_state
        from code_indexer.server.utils.config_manager import TelemetryConfig

        config = TelemetryConfig(enabled=True, export_traces=True)
        get_telemetry_manager(config)
        try:
            with create_span("test.inject_trace_context"):
                record = _make_log_record()
                inject_trace_context(record)

                assert len(record.trace_id) == 32
                assert len(record.span_id) == 16
                # Must be genuinely different from the zero-values (a real,
                # recording span was active) and valid hex.
                assert record.trace_id != "0" * 32
                assert record.span_id != "0" * 16
                int(record.trace_id, 16)
                int(record.span_id, 16)
        finally:
            reset_spans_state()
            reset_telemetry_manager()
