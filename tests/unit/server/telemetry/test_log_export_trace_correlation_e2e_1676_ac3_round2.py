"""
Story #1676 AC3 -- code review round 2, REQUIRED FIX 2.

The single production line that makes real OTLP trace/span correlation
work -- ``inject_otel_context(record)`` inside
``async_logging.IdentityQueueHandler.prepare()`` -- had zero test coverage
that actually exercises the real end-to-end pipeline: every pre-existing
test either called ``inject_otel_context()`` directly, or hand-planted the
private context attribute on a record with ``setattr(...)``. Deleting that
one production line makes AC3 functionally dead (every exported OTLP
LogRecord gets trace_id=0/span_id=0) while every pre-existing test in the
commit still passes.

This test wires the REAL production pipeline end-to-end:

    root logger -> IdentityQueueHandler -> queue -> DrainableQueueListener
    -> ContextAwareLogBridgeHandler -> real OTEL logging-instrumentation
    LoggingHandler -> real LoggerProvider -> InMemoryLogRecordExporter

A span is created and a log line is emitted on a SEPARATE worker thread
(mirroring the cross-thread hazard this whole mechanism exists to solve:
OTEL's "current context" is contextvars-based and does NOT propagate across
a plain ``threading.Thread`` boundary -- see ``inject_otel_context()``'s and
``ContextAwareLogBridgeHandler``'s docstrings). The exported OTLP
LogRecord's trace_id/span_id are asserted to match the span that was active
on that original worker thread, NOT whatever (unrelated) context is live on
the async-logging listener thread that ultimately performs the export.

Real OTEL SDK objects throughout (Messi Rule #1: no mocks of the thing
under test) -- TelemetryManager, LoggerProvider, InMemoryLogRecordExporter,
QueueListener, and the real ``opentelemetry-instrumentation-logging``
bridge handler are all genuine.
"""

from __future__ import annotations

import logging
import threading


def _install_in_memory_log_exporter(logger_provider):
    """Attach a real InMemoryLogRecordExporter to ``logger_provider`` via a
    SimpleLogRecordProcessor (synchronous export -- no batching delay) and
    return the exporter for inspection.
    """
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )

    exporter = InMemoryLogRecordExporter()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return exporter


def _emit_log_line_inside_a_span_on_a_worker_thread(
    logger_name: str, message: str, span_name: str
):
    """Run ``create_span()`` + one log call on a SEPARATE thread, returning
    (span_context, thread) so the caller can assert the export result
    against the span that was genuinely active on that OTHER thread.
    """
    from code_indexer.server.telemetry.spans import create_span

    result = {}

    def _worker() -> None:
        with create_span(span_name) as span:
            result["span_context"] = span.get_span_context()
            logging.getLogger(logger_name).error(message)

    thread = threading.Thread(target=_worker, name="otel-export-e2e-worker")
    thread.start()
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "worker thread did not finish in time"
    return result["span_context"]


class TestOtelContextPropagatesToRealExportedLogRecordAcrossThreads:
    def test_exported_log_record_carries_the_worker_threads_span_ids(
        self,
    ) -> None:
        from code_indexer.server.services.async_logging import (
            install_queue_logging,
            shutdown_queue_logging,
        )
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )
        from code_indexer.server.telemetry.spans import reset_spans_state
        from code_indexer.server.utils.config_manager import TelemetryConfig

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        message = "otel export e2e cross-thread correlation marker"

        try:
            root.handlers = []
            root.setLevel(logging.INFO)

            # install_queue_logging() MUST run before get_telemetry_manager()
            # -- it is what makes _active_listener non-None, which is what
            # TelemetryManager's log bridge registration attaches to.
            listener = install_queue_logging([])
            try:
                # Bug #1744 investigation: export_traces=False here (not
                # True) is deliberate, not an oversight. It removes ONE of
                # two real, unreachable-network OTLP exporters this test
                # used to construct. Per the already-documented Bug #1679
                # invariant (test_request_tracing.py::
                # test_instrument_fastapi_no_export_when_traces_disabled),
                # export_traces=False still gives a real, valid, recording
                # TracerProvider -- _setup_trace_exporter() is simply never
                # called, so create_span() below still produces a genuine
                # span (span_context.is_valid still holds), with zero span
                # processors attached and nothing to flush on shutdown.
                #
                # This alone does NOT make the test fast, and is not meant
                # to: the dominant remaining cost (~6-9s, confirmed live)
                # is reset_telemetry_manager() -> LoggerProvider.shutdown()
                # flushing the REAL BatchLogRecordProcessor(OTLPLogExporter)
                # that _setup_log_exporter() constructs whenever
                # export_logs=True, once this test has actually enqueued a
                # real log record into it. That dependency is INHERENT to
                # this test's actual subject -- the real
                # _register_log_bridge_handler()/LoggerProvider wiring that
                # Story #1676 AC3 round 2 exists specifically to cover end
                # to end -- and export_logs=True cannot be dropped without
                # defeating that purpose. There is currently no config
                # switch (unlike export_traces) that separates "construct a
                # real LoggerProvider + register the log bridge handler"
                # from "also attach a real network OTLP log exporter" --
                # decoupling those would need a TelemetryManager change,
                # out of scope for a test-only fix. This test is therefore
                # judged a genuinely different class of #1744 sibling
                # (structural network dependency on its own subject, not a
                # swappable trace-only artifact) and is correctly left with
                # a real, if now singular, network exporter.
                config = TelemetryConfig(
                    enabled=True,
                    export_traces=False,
                    export_metrics=False,
                    export_logs=True,
                )
                manager = get_telemetry_manager(config)
                try:
                    assert manager.logger_provider is not None
                    exporter = _install_in_memory_log_exporter(manager.logger_provider)

                    span_context = _emit_log_line_inside_a_span_on_a_worker_thread(
                        logger_name="test.otel_export_e2e.cross_thread",
                        message=message,
                        span_name="test.otel_export_e2e.worker_span",
                    )
                    assert span_context.is_valid

                    listener.flush()

                    matching = [
                        ld.log_record
                        for ld in exporter.get_finished_logs()
                        if ld.log_record.body == message
                    ]
                    assert len(matching) == 1, (
                        f"expected exactly one exported log record with the "
                        f"marker message, got {len(matching)}"
                    )
                    exported = matching[0]

                    assert exported.trace_id == span_context.trace_id, (
                        "exported OTLP LogRecord.trace_id must match the "
                        "span active on the ORIGINAL worker thread, not "
                        "whatever context is live on the listener thread"
                    )
                    assert exported.span_id == span_context.span_id, (
                        "exported OTLP LogRecord.span_id must match the "
                        "span active on the ORIGINAL worker thread, not "
                        "whatever context is live on the listener thread"
                    )
                finally:
                    reset_telemetry_manager()
            finally:
                listener.stop()
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)
            shutdown_queue_logging()
            reset_spans_state()
