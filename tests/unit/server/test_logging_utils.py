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
        """Bug #1744: this test used to construct a real TelemetryConfig
        (enabled=True, export_traces=True) via get_telemetry_manager(),
        which builds a real BatchSpanProcessor backed by an OTLP gRPC
        exporter pointed at localhost:4317. In any environment without a
        local OTEL collector listening there, tearing that manager down
        (reset_telemetry_manager() -> shutdown()) forces a real export
        attempt that retries against the unreachable endpoint before
        giving up -- adding ~14-16s of wall-clock blocking to what should
        be a sub-second unit test, and making the test's outcome sensitive
        to whatever timing pressure a full-suite run applies (confirmed:
        15.31s solo runtime with "Failed to export traces to
        localhost:4317, error code: StatusCode.UNAVAILABLE" logged).

        Fixed by using active_span_exporter() (Story #1586 AC5 pattern,
        already established in tests/unit/server/telemetry/
        otel_test_support.py and reused by test_trace_sampling_1676_ac4.py)
        instead: a real, locally-owned TracerProvider backed by a real
        InMemorySpanExporter, installed directly into spans.py's tracer
        cache. This exercises the exact same real create_span()/
        inject_trace_context()/get_trace_context() production code path
        against a real OTEL Span and Context -- no mocking of the code
        under test -- with zero network I/O, so no collector dependency
        and no wall-clock sensitivity.
        """
        from code_indexer.server.logging_utils import inject_trace_context
        from code_indexer.server.telemetry.spans import create_span
        from tests.unit.server.telemetry.otel_test_support import (
            active_span_exporter,
        )

        with active_span_exporter():
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


class TestInjectOtelContext:
    """Story #1676 AC3: inject_otel_context() captures the full OTEL
    ``Context`` object active on the calling thread and attaches it to the
    LogRecord as a private attribute. This is the wiring point
    (async_logging.IdentityQueueHandler.prepare()) that later lets the
    context-aware log bridge handler reattach the ORIGINAL request thread's
    context on the QueueListener thread before exporting -- producing the
    correct trace_id/span_id on the exported OTLP LogRecord instead of
    whatever (unrelated) context happens to be live on the listener thread.
    """

    def test_attaches_private_context_attribute(self):
        from code_indexer.server.logging_utils import (
            OTEL_CONTEXT_RECORD_ATTR,
            inject_otel_context,
        )

        record = _make_log_record()
        assert not hasattr(record, OTEL_CONTEXT_RECORD_ATTR)

        inject_otel_context(record)

        assert hasattr(record, OTEL_CONTEXT_RECORD_ATTR)

    def test_captured_context_is_the_real_ambient_context_object(self):
        from opentelemetry import context as otel_context

        from code_indexer.server.logging_utils import (
            OTEL_CONTEXT_RECORD_ATTR,
            inject_otel_context,
        )

        record = _make_log_record()
        inject_otel_context(record)

        captured = getattr(record, OTEL_CONTEXT_RECORD_ATTR)
        # Identity, not mere type equivalence: proves this is the SAME
        # ambient Context object context.get_current() resolves to on this
        # thread right now, not an equivalent copy/synthetic stand-in.
        assert captured is otel_context.get_current()

    def test_does_not_override_already_captured_context(self):
        from code_indexer.server.logging_utils import (
            OTEL_CONTEXT_RECORD_ATTR,
            inject_otel_context,
        )

        record = _make_log_record()
        sentinel = object()
        setattr(record, OTEL_CONTEXT_RECORD_ATTR, sentinel)

        inject_otel_context(record)

        assert getattr(record, OTEL_CONTEXT_RECORD_ATTR) is sentinel


class TestInjectOtelContextReflectsActiveSpan:
    """Discriminating test: the captured Context when a span IS active must
    be the real ambient Context carrying that span (identity-checked, not
    just isinstance-checked) and must differ from the no-span case -- proving
    this genuinely reads ambient state rather than always stashing a fixed/
    empty Context object regardless of what is actually active on the
    calling thread."""

    def test_captured_context_carries_the_real_active_span_by_identity(self):
        """Bug #1744 sibling: same fix as
        TestInjectTraceContext.test_sets_real_ids_from_active_span above --
        this test shared the identical real-TelemetryConfig/
        get_telemetry_manager() setup (confirmed 15.55s solo runtime with
        the same unreachable-localhost:4317 OTLP export on teardown).
        Replaced with active_span_exporter() for the same reason: real
        Span/Context objects, zero network I/O.
        """
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace

        from code_indexer.server.logging_utils import (
            OTEL_CONTEXT_RECORD_ATTR,
            inject_otel_context,
        )
        from code_indexer.server.telemetry.spans import create_span
        from tests.unit.server.telemetry.otel_test_support import (
            active_span_exporter,
        )

        with active_span_exporter():
            record_without_span = _make_log_record()
            inject_otel_context(record_without_span)
            context_without_span = getattr(
                record_without_span, OTEL_CONTEXT_RECORD_ATTR
            )

            with create_span("test.inject_otel_context.active"):
                record_with_span = _make_log_record()
                inject_otel_context(record_with_span)
                context_with_span = getattr(record_with_span, OTEL_CONTEXT_RECORD_ATTR)
                # Identity check while still inside the span's `with` block,
                # where context.get_current() is guaranteed to still equal
                # the exact object just captured.
                assert context_with_span is otel_context.get_current()

            # Extract the span recorded in each captured Context -- proves
            # the capture reflects the REAL ambient span, not a fixed value.
            span_without = otel_trace.get_current_span(context_without_span)
            span_with = otel_trace.get_current_span(context_with_span)

            assert not span_without.get_span_context().is_valid
            assert span_with.get_span_context().is_valid
