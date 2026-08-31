"""Story #1676 AC2: LogAggregatorService's local/direct-SQLite path (solo
mode) must surface trace_id/span_id in query()/query_all() results.

Real SQLiteLogHandler write path -> real LogAggregatorService read path,
no mocks (Messi Rule #1). Confirms the local-path SELECT/row-mapping
extension made in log_aggregator_service.py actually round-trips end to
end, not just that the SQL text looks right.
"""

from __future__ import annotations

import logging
from pathlib import Path


def _write_one_log_row(db_path: Path, message: str, level: int = logging.ERROR) -> None:
    """Write a single log record through the real SQLiteLogHandler and
    flush its writer thread so the row is durably persisted before the
    test reads it back."""
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    handler = SQLiteLogHandler(db_path=db_path)
    try:
        record = logging.LogRecord(
            name="test.log_aggregator.trace_span",
            level=level,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        handler.flush()
    finally:
        handler.close()


class TestLogAggregatorServiceLocalPathTraceSpan:
    def test_query_returns_zero_trace_span_when_no_active_span(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )

        db_path = tmp_path / "logs.db"
        _write_one_log_row(db_path, "aggregator no-span line")

        service = LogAggregatorService(db_path)
        result = service.query(search="aggregator no-span line")

        assert len(result["logs"]) == 1
        log_entry = result["logs"][0]
        assert log_entry["trace_id"] == "0" * 32
        assert log_entry["span_id"] == "0" * 16

    def test_query_returns_real_trace_span_when_span_active(
        self, tmp_path: Path
    ) -> None:
        """Bug #1744 sibling: this test used to construct a real
        TelemetryConfig(enabled=True, export_traces=True) via
        get_telemetry_manager(), whose teardown (reset_telemetry_manager()
        -> shutdown()) forces a real OTLP export attempt against an
        unreachable localhost:4317 collector -- confirmed 13.56s solo
        runtime, same root cause class as
        test_logging_utils.py::TestInjectTraceContext::
        test_sets_real_ids_from_active_span. Fixed with the same
        active_span_exporter() in-memory mechanism: real Span/Context,
        zero network I/O.
        """
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )
        from code_indexer.server.telemetry.spans import create_span
        from tests.unit.server.telemetry.otel_test_support import (
            active_span_exporter,
        )

        db_path = tmp_path / "logs.db"
        with active_span_exporter():
            with create_span("test.log_aggregator.active_span"):
                _write_one_log_row(db_path, "aggregator active-span line")

        service = LogAggregatorService(db_path)
        result = service.query(search="aggregator active-span line")

        assert len(result["logs"]) == 1
        log_entry = result["logs"][0]
        assert len(log_entry["trace_id"]) == 32
        assert len(log_entry["span_id"]) == 16
        assert log_entry["trace_id"] != "0" * 32
        assert log_entry["span_id"] != "0" * 16

    def test_query_all_includes_trace_span(self, tmp_path: Path) -> None:
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )

        db_path = tmp_path / "logs.db"
        _write_one_log_row(db_path, "aggregator query_all line")

        service = LogAggregatorService(db_path)
        logs = service.query_all(search="aggregator query_all line")

        assert len(logs) == 1
        assert logs[0]["trace_id"] == "0" * 32
        assert logs[0]["span_id"] == "0" * 16
