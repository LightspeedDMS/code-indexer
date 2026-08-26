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
import threading
import time
from typing import TYPE_CHECKING, Optional

import pytest

from code_indexer.server.services.async_logging import (
    install_queue_logging,
    shutdown_queue_logging,
)

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


class _CountingHandler(logging.Handler):
    """Fake wrapped handler: just counts how many times emit() was invoked."""

    def __init__(self) -> None:
        super().__init__()
        self.emit_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.emit_count += 1


# Test-only safety cap (NOT production code): bounds the self-amplifying
# loop in _AmplifyingFakeOtelBridgeHandler below so a pre-fix run terminates
# deterministically instead of looping forever.
_AMPLIFICATION_SAFETY_CAP = 5

# How long to let the real async-logging pipeline settle before sampling
# the final count, and how often to poll while waiting.
_PIPELINE_SETTLE_TIMEOUT_SECONDS = 2.0
_PIPELINE_POLL_INTERVAL_SECONDS = 0.05


class _AmplifyingFakeOtelBridgeHandler(logging.Handler):
    """Stand-in for the REAL opentelemetry-instrumentation-logging bridge
    handler's collector-unreachable failure path. On EVERY emit it mimics
    the OTEL SDK's own documented behavior (``opentelemetry.sdk.
    _shared_internal`` / ``opentelemetry.exporter.otlp.proto.grpc.exporter``
    both do this on export failure): it logs a WARNING through one of the
    OTEL SDK's OWN loggers. Since that record flows back through the SAME
    root logger -> async-logging queue -> listener -> the wrapper handler
    under test, an unfiltered wrapper causes this handler's ``emit()`` to be
    invoked again for the self-generated warning -- and again, and again.
    ``_AMPLIFICATION_SAFETY_CAP`` bounds how many times it will re-trigger
    (a TEST safety bound, NOT production code) so a pre-fix run terminates
    deterministically instead of looping forever, while still proving
    genuine amplification: post-fix, the count stays at 1 (the filter stops
    the loop after the very first application log line); pre-fix, it grows
    to the safety cap.

    ``emit()`` runs on the async-logging listener thread while the test's
    main thread polls the count concurrently -- a ``threading.Lock`` guards
    every read/write so the settling loop and final assertion never observe
    a torn or stale value.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._emit_count = 0
        self._otel_logger = logging.getLogger(
            "opentelemetry.exporter.otlp.proto.grpc.exporter"
        )

    @property
    def emit_count(self) -> int:
        with self._lock:
            return self._emit_count

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self._emit_count += 1
            current_count = self._emit_count
        if current_count < _AMPLIFICATION_SAFETY_CAP:
            self._otel_logger.warning(
                "simulated collector-unreachable export failure #%d",
                current_count,
            )


def _run_dead_collector_pipeline() -> "_AmplifyingFakeOtelBridgeHandler":
    """Wire the REAL async-logging queue/listener pipeline with the
    amplifying fake bridge handler, emit ONE real application log line, let
    the pipeline settle, and return the fake handler for inspection.

    Saves/restores root logger state so this cannot leak into other tests.
    """
    fake_bridge = _AmplifyingFakeOtelBridgeHandler()
    wrapper = ContextAwareLogBridgeHandler(fake_bridge)

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        root.handlers = []
        root.setLevel(logging.INFO)
        listener = install_queue_logging([wrapper])
        try:
            logging.getLogger("test.app.dead_collector_repro").info(
                "one real application log line"
            )

            deadline = time.monotonic() + _PIPELINE_SETTLE_TIMEOUT_SECONDS
            last_count = -1
            while time.monotonic() < deadline:
                listener.flush()
                current_count = fake_bridge.emit_count
                if current_count == last_count:
                    break
                last_count = current_count
                time.sleep(_PIPELINE_POLL_INTERVAL_SECONDS)
        finally:
            listener.stop()
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        shutdown_queue_logging()

    return fake_bridge


def _otel_named_record(logger_name: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name,
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Queue full, dropping log records",
        args=(),
        exc_info=None,
    )


class TestContextAwareLogBridgeHandlerFiltersOtelInternalLoggers:
    """Round 2 REQUIRED FIX 1: the OTEL SDK's/exporter's own diagnostic
    loggers (e.g. ``opentelemetry.sdk._shared_internal``,
    ``opentelemetry.exporter.otlp.proto.grpc.exporter`` -- both confirmed
    live via source inspection to use ``logging.getLogger(__name__)``, i.e.
    a dotted name always starting with ``opentelemetry``) must NEVER be
    re-exported by this wrapper. Without this filter, a record emitted by
    one of those loggers (e.g. "Queue full, dropping..." or a per-retry
    "failed to export" warning when the collector is unreachable) would
    propagate through the root logger -> async-logging queue -> listener ->
    this SAME wrapper handler -> the real (failing) OTLP exporter -> which
    logs ANOTHER warning through its own ``opentelemetry.*`` logger --
    forever. That self-amplifying feedback loop is the exact production
    defect this test guards against.
    """

    def test_wrapped_handler_never_invoked_for_opentelemetry_named_records(
        self,
    ) -> None:
        wrapped = _CountingHandler()
        wrapper = ContextAwareLogBridgeHandler(wrapped)

        for logger_name in (
            "opentelemetry",
            "opentelemetry.sdk._shared_internal",
            "opentelemetry.exporter.otlp.proto.grpc.exporter",
            "opentelemetry.exporter.otlp.proto.http._log_exporter",
        ):
            wrapper.emit(_otel_named_record(logger_name))

        assert wrapped.emit_count == 0, (
            "OTEL SDK/exporter diagnostic loggers must never reach the "
            "wrapped bridge handler -- doing so re-triggers the export "
            "machinery and causes a self-amplifying feedback loop"
        )

    def test_application_logger_records_are_unaffected_by_the_filter(
        self,
    ) -> None:
        wrapped = _CountingHandler()
        wrapper = ContextAwareLogBridgeHandler(wrapped)

        record = logging.LogRecord(
            name="code_indexer.server.some_real_module",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="a genuine application log line",
            args=(),
            exc_info=None,
        )
        wrapper.emit(record)

        assert wrapped.emit_count == 1

    def test_dead_collector_does_not_self_amplify_through_the_real_pipeline(
        self,
    ) -> None:
        fake_bridge = _run_dead_collector_pipeline()

        assert fake_bridge.emit_count == 1, (
            "expected exactly ONE emit (the real application log line) -- "
            "the OTEL SDK's own self-generated diagnostic warnings must "
            "never re-enter the bridge and amplify, but got "
            f"{fake_bridge.emit_count}"
        )
