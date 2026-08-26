"""
Story #1676 AC3 Requirement 5: ContextAwareLogBridgeHandler is the ONE
wrapper handler registered with the async-logging listener via
register_additional_listener_handler(). On the QueueListener thread, in
order, it must:

  1. Retrieve the record's captured private Context attribute
  2. Remove/pop it from the record (so it can never leak into either
     serialization path -- SQLite/PostgreSQL JSON, or OTLP attribute
     translation)
  3. context.attach() the captured context
  4. Invoke the real opentelemetry-instrumentation-logging handler's emit()
  5. context.detach() in a finally block regardless of step 4's outcome

This is the core correlation mechanism: it makes the exported OTLP
LogRecord's trace_id/span_id come from the span active on the ORIGINAL
request thread at log-call time, not whatever (unrelated) context happens
to be live on the listener thread when this handler actually runs.

Beyond the literal 5 steps, the wrapper must ALSO never let a wrapped
handler's exception propagate: ``logging.handlers.QueueListener._monitor()``
(confirmed via source inspection) has NO try/except around
``self.handle(record)`` -- an unhandled exception here would kill the
shared async-logging background thread for the whole process, silently
dropping ALL subsequent log records. The wrapper reports failures via
``self.handleError(record)`` instead, mirroring
``async_logging.IdentityQueueHandler.emit()``'s own established pattern.

All tests use real opentelemetry.context/trace primitives (Messi Rule #1:
no mocks of the thing under test) -- only the WRAPPED handler is a small
local fake so we can observe what it received without a live OTLP export.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import pytest

from code_indexer.server.logging_utils import OTEL_CONTEXT_RECORD_ATTR
from code_indexer.server.telemetry.log_handler import ContextAwareLogBridgeHandler

if TYPE_CHECKING:
    from opentelemetry.trace import SpanContext


def _make_record(msg: str = "context aware handler test") -> logging.LogRecord:
    return logging.LogRecord(
        name="test.telemetry.context_aware_log_handler",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class _SpanObservingHandler(logging.Handler):
    """Fake wrapped handler: records the ambient span context AND whether
    the private context attribute was still present on the record, at the
    moment emit() was invoked."""

    def __init__(self) -> None:
        super().__init__()
        self.observed_span_context: Optional["SpanContext"] = None
        self.saw_private_context_attr: Optional[bool] = None
        self.emit_call_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        from opentelemetry import trace as otel_trace

        self.emit_call_count += 1
        self.saw_private_context_attr = hasattr(record, OTEL_CONTEXT_RECORD_ATTR)
        self.observed_span_context = otel_trace.get_current_span().get_span_context()


class _RaisingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("simulated wrapped handler failure")


@pytest.fixture(autouse=True)
def _reset_telemetry_state():
    yield
    from code_indexer.server.telemetry import reset_telemetry_manager
    from code_indexer.server.telemetry.spans import reset_spans_state

    reset_spans_state()
    reset_telemetry_manager()


class TestContextAwareLogBridgeHandlerStripsPrivateAttribute:
    def test_private_context_attribute_is_removed_before_wrapped_emit(self) -> None:
        from opentelemetry import context as otel_context

        record = _make_record()
        setattr(record, OTEL_CONTEXT_RECORD_ATTR, otel_context.get_current())
        wrapped = _SpanObservingHandler()
        wrapper = ContextAwareLogBridgeHandler(wrapped)

        wrapper.emit(record)

        assert wrapped.emit_call_count == 1
        assert wrapped.saw_private_context_attr is False


class TestContextAwareLogBridgeHandlerReattachesCapturedContext:
    def test_reattach_produces_the_correct_span_context_at_export_time(
        self,
    ) -> None:
        from opentelemetry import trace as otel_trace

        from code_indexer.server.telemetry import get_telemetry_manager
        from code_indexer.server.telemetry.spans import create_span
        from code_indexer.server.utils.config_manager import TelemetryConfig

        config = TelemetryConfig(enabled=True, export_traces=True)
        get_telemetry_manager(config)

        from opentelemetry import context as otel_context

        with create_span("test.context_aware_handler.span_a") as span_a:
            span_a_context = span_a.get_span_context()
            captured = otel_context.get_current()

        # Outside span A's `with` block now -- ambient context has no
        # active span, simulating the unrelated listener-thread context.
        assert not otel_trace.get_current_span().get_span_context().is_valid

        record = _make_record()
        setattr(record, OTEL_CONTEXT_RECORD_ATTR, captured)
        wrapped = _SpanObservingHandler()
        wrapper = ContextAwareLogBridgeHandler(wrapped)

        wrapper.emit(record)

        assert wrapped.observed_span_context is not None
        assert wrapped.observed_span_context.trace_id == span_a_context.trace_id
        assert wrapped.observed_span_context.span_id == span_a_context.span_id
        # detach() ran: ambient context is restored after emit() returns.
        assert not otel_trace.get_current_span().get_span_context().is_valid

    def test_context_is_detached_even_when_wrapped_emit_raises(self) -> None:
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace

        from code_indexer.server.telemetry import get_telemetry_manager
        from code_indexer.server.telemetry.spans import create_span
        from code_indexer.server.utils.config_manager import TelemetryConfig

        config = TelemetryConfig(enabled=True, export_traces=True)
        get_telemetry_manager(config)

        with create_span("test.context_aware_handler.raising"):
            captured = otel_context.get_current()

        assert not otel_trace.get_current_span().get_span_context().is_valid

        record = _make_record()
        setattr(record, OTEL_CONTEXT_RECORD_ATTR, captured)
        wrapper = ContextAwareLogBridgeHandler(_RaisingHandler())

        # Must NOT propagate: an unhandled exception here would kill the
        # shared async-logging listener thread for the whole process (see
        # module docstring -- confirmed via source inspection of
        # QueueListener._monitor()).
        wrapper.emit(record)

        # detach() still ran despite the wrapped handler raising -- no
        # context leak onto whatever runs next on this thread.
        assert not otel_trace.get_current_span().get_span_context().is_valid


class TestContextAwareLogBridgeHandlerNoCapturedContext:
    def test_emits_normally_when_record_has_no_captured_context(self) -> None:
        record = _make_record()
        assert not hasattr(record, OTEL_CONTEXT_RECORD_ATTR)
        wrapped = _SpanObservingHandler()
        wrapper = ContextAwareLogBridgeHandler(wrapped)

        wrapper.emit(record)

        assert wrapped.emit_call_count == 1
