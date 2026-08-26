"""Story #1676 AC2: IdentityQueueHandler.prepare() must inject trace/span
context onto the calling thread, mirroring the Bug #1641 correlation_id
wiring exactly.

Rationale: OTEL's "current span" is resolved via contextvars inside
get_trace_context(). Once a LogRecord crosses into the QueueListener's
persistent background thread (a plain threading.Thread), that context is no
longer visible. IdentityQueueHandler.prepare() runs synchronously on the
ORIGINAL calling (request) thread -- the last point before the record is
handed off via the queue -- so it is the correct central place to inject
record.trace_id/record.span_id.
"""

from __future__ import annotations

import logging
import queue

import pytest

from code_indexer.server.services.async_logging import IdentityQueueHandler


def _make_record(msg: str = "test message") -> logging.LogRecord:
    return logging.LogRecord(
        name="test.async_logging.trace_context",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class TestIdentityQueueHandlerPrepareInjectsTraceContext:
    """Unit-level proof at the exact wiring point (runs on the calling
    thread, before the record crosses into the async queue)."""

    def test_prepare_injects_zero_trace_context_when_no_active_span(self) -> None:
        q: "queue.Queue" = queue.Queue()
        handler = IdentityQueueHandler(q)
        record = _make_record()

        assert not hasattr(record, "trace_id")
        assert not hasattr(record, "span_id")

        prepared = handler.prepare(record)

        assert prepared is record, "prepare() must still return the same object"
        assert prepared.trace_id == "0" * 32
        assert prepared.span_id == "0" * 16

    def test_prepare_does_not_override_existing_trace_context(self) -> None:
        q: "queue.Queue" = queue.Queue()
        handler = IdentityQueueHandler(q)
        record = _make_record()
        record.trace_id = "already-set-trace-id"
        record.span_id = "already-set-span-id"

        prepared = handler.prepare(record)

        assert prepared.trace_id == "already-set-trace-id"
        assert prepared.span_id == "already-set-span-id"

    def test_prepare_injects_real_trace_context_from_active_span(self) -> None:
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )
        from code_indexer.server.telemetry.spans import create_span, reset_spans_state
        from code_indexer.server.utils.config_manager import TelemetryConfig

        config = TelemetryConfig(enabled=True, export_traces=True)
        get_telemetry_manager(config)
        try:
            q: "queue.Queue" = queue.Queue()
            handler = IdentityQueueHandler(q)

            with create_span("test.async_logging.prepare"):
                record = _make_record()
                prepared = handler.prepare(record)

                assert len(prepared.trace_id) == 32
                assert len(prepared.span_id) == 16
                assert prepared.trace_id != "0" * 32
                assert prepared.span_id != "0" * 16
        finally:
            reset_spans_state()
            reset_telemetry_manager()

    def test_prepare_injects_otel_context(self) -> None:
        """AC3 REQUIRED FIX 2: prepare() must capture the full OTEL Context
        (not just trace_id/span_id strings) via inject_otel_context(), so
        it can be reattached at export time. Fails if that call is removed
        from prepare() -- captured would then be None.
        """
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace

        from code_indexer.server.logging_utils import OTEL_CONTEXT_RECORD_ATTR
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )
        from code_indexer.server.telemetry.spans import create_span, reset_spans_state
        from code_indexer.server.utils.config_manager import TelemetryConfig

        config = TelemetryConfig(enabled=True, export_traces=True)
        get_telemetry_manager(config)
        try:
            q: "queue.Queue" = queue.Queue()
            handler = IdentityQueueHandler(q)

            with create_span("test.async_logging.prepare_otel_context") as span:
                span_context = span.get_span_context()
                record = _make_record()
                prepared = handler.prepare(record)

            captured = getattr(prepared, OTEL_CONTEXT_RECORD_ATTR, None)
            assert captured is not None

            token = otel_context.attach(captured)
            try:
                reattached = otel_trace.get_current_span().get_span_context()
                assert reattached.trace_id == span_context.trace_id
                assert reattached.span_id == span_context.span_id
            finally:
                otel_context.detach(token)
        finally:
            reset_spans_state()
            reset_telemetry_manager()


class TestPlainLogCallPersistsTraceContextViaAsyncQueue:
    """Real production path: root logger -> IdentityQueueHandler ->
    DrainableQueueListener -> SQLiteLogHandler, exactly as wired by
    startup/lifespan.py's install_queue_logging(...) call."""

    def test_plain_logger_call_with_no_active_span_persists_zero_values(
        self, tmp_path
    ) -> None:
        import sqlite3

        from code_indexer.server.services.async_logging import install_queue_logging
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        db_path = tmp_path / "logs.db"
        sqlite_handler = SQLiteLogHandler(db_path=db_path)

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            root.handlers = [sqlite_handler]
            root.setLevel(logging.INFO)
            listener = install_queue_logging([sqlite_handler])
            try:
                logging.getLogger("test.async_logging.trace_context").info(
                    "no span active line"
                )
                listener.flush()
            finally:
                listener.stop()

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT trace_id, span_id FROM logs WHERE message LIKE "
                    "'%no span active line%'"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None, "log row was not persisted at all"
            assert row[0] == "0" * 32, f"expected zero trace_id, got {row[0]!r}"
            assert row[1] == "0" * 16, f"expected zero span_id, got {row[1]!r}"
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)
            sqlite_handler.close()

    def test_plain_logger_call_with_active_span_persists_real_ids(
        self, tmp_path
    ) -> None:
        import sqlite3

        from code_indexer.server.services.async_logging import install_queue_logging
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )
        from code_indexer.server.telemetry.spans import create_span, reset_spans_state
        from code_indexer.server.utils.config_manager import TelemetryConfig

        config = TelemetryConfig(enabled=True, export_traces=True)
        get_telemetry_manager(config)

        db_path = tmp_path / "logs.db"
        sqlite_handler = SQLiteLogHandler(db_path=db_path)

        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        try:
            root.handlers = [sqlite_handler]
            root.setLevel(logging.INFO)
            listener = install_queue_logging([sqlite_handler])
            try:
                with create_span("test.async_logging.persist_real_ids"):
                    logging.getLogger("test.async_logging.trace_context").error(
                        "active span error line"
                    )
                listener.flush()
            finally:
                listener.stop()

            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT trace_id, span_id FROM logs WHERE message LIKE "
                    "'%active span error line%'"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None, "log row was not persisted at all"
            assert row[0] is not None and len(row[0]) == 32
            assert row[1] is not None and len(row[1]) == 16
            assert row[0] != "0" * 32
            assert row[1] != "0" * 16
        finally:
            root.handlers = saved_handlers
            root.setLevel(saved_level)
            sqlite_handler.close()
            reset_spans_state()
            reset_telemetry_manager()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
