"""Story #1676 AC2: handle_admin_logs_query must surface trace_id/span_id.

Mirrors test_admin_logs_query_cluster_read_1553.py's exact pattern (real
SQLiteLogHandler write path, real LogsSqliteBackend subclass tagged
cross-node, app_module patched per the established MCP handler test
pattern) but specifically verifies the two new columns round-trip through
the cluster-mode backend-dispatched read path -- the exact class of bug
this story calls out (#1653/#1654/#1662): a local-only fix that leaves
PostgreSQL-backed admin_logs_query silently omitting new fields.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

from .conftest import extract_mcp_data


class _FakeClusterBackend(LogsSqliteBackend):
    """Real SQLite-backed LogsBackend tagged as cross-node for tests."""

    is_cross_node_backend: bool = True


def _write_one_log(
    tmp_path,
    message: str,
    *,
    logs_backend: Optional[Any] = None,
    active_span_name: Optional[str] = None,
) -> None:
    """Write one ERROR log record through a real SQLiteLogHandler,
    optionally delegated to logs_backend and/or inside an active span."""
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

    handler = SQLiteLogHandler(db_path=tmp_path / "logs.db", logs_backend=logs_backend)
    try:
        record = logging.LogRecord(
            name="test.mcp.trace_span",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        if active_span_name is not None:
            from code_indexer.server.telemetry.spans import create_span

            with create_span(active_span_name):
                handler.emit(record)
        else:
            handler.emit(record)
        handler.flush()
    finally:
        handler.close()


def _admin_user():
    from datetime import datetime, timezone

    from code_indexer.server.auth.user_manager import User, UserRole

    # "not-a-real-hash" is a placeholder for a field the handlers under test
    # never read (only user.role is checked) -- no real credential material.
    return User(
        username="admin-1676-mcp",
        role=UserRole.ADMIN,
        password_hash="not-a-real-hash",
        created_at=datetime.now(timezone.utc),
    )


@contextmanager
def _wired_app_module(tmp_path, logs_backend: Optional[Any]) -> Iterator[None]:
    """Patch code_indexer.server.mcp.handlers._utils.app_module to point at
    tmp_path's logs.db and the given (possibly None) logs_backend, mirroring
    the established MCP handler test pattern."""
    from unittest.mock import patch

    with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app_module:
        mock_app_module.app.state.log_db_path = str(tmp_path / "logs.db")
        mock_app_module.app.state.logs_backend = logs_backend
        yield


def _enable_real_telemetry() -> None:
    from code_indexer.server.telemetry import get_telemetry_manager
    from code_indexer.server.utils.config_manager import TelemetryConfig

    get_telemetry_manager(TelemetryConfig(enabled=True, export_traces=True))


def _reset_spans() -> None:
    """Reset both span state and the telemetry manager singleton.

    #1676 AC2 round 2 code review REQUIRED FIX 2: _enable_real_telemetry()
    installs a process-wide TelemetryManager singleton (real OTLP gRPC
    exporter). Resetting only reset_spans_state() left that singleton alive
    for every subsequent test in the same pytest process, causing exporter
    retry noise against a dead localhost:4317 to leak into unrelated tests.
    """
    from code_indexer.server.telemetry import reset_telemetry_manager
    from code_indexer.server.telemetry.spans import reset_spans_state

    reset_spans_state()
    reset_telemetry_manager()


class TestAdminLogsQueryLocalPathTraceSpan:
    def test_no_active_span_returns_zero_values(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query

        _write_one_log(tmp_path, "mcp-local-no-span-1676")

        with _wired_app_module(tmp_path, logs_backend=None):
            result = extract_mcp_data(
                handle_admin_logs_query(
                    {"search": "mcp-local-no-span-1676"}, _admin_user()
                )
            )

        assert result["success"] is True
        assert len(result["logs"]) == 1
        entry = result["logs"][0]
        assert entry["trace_id"] == "0" * 32
        assert entry["span_id"] == "0" * 16


class TestAdminLogsQueryClusterPathTraceSpan:
    """The trap this story explicitly calls out: cluster-mode backend
    dispatch must not silently omit trace_id/span_id."""

    def test_cluster_backend_record_includes_zero_trace_span(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query

        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        _write_one_log(
            tmp_path, "mcp-cluster-no-span-1676", logs_backend=cluster_backend
        )

        with _wired_app_module(tmp_path, logs_backend=cluster_backend):
            result = extract_mcp_data(
                handle_admin_logs_query(
                    {"search": "mcp-cluster-no-span-1676"}, _admin_user()
                )
            )

        assert result["success"] is True
        assert len(result["logs"]) == 1
        entry = result["logs"][0]
        assert entry["trace_id"] == "0" * 32
        assert entry["span_id"] == "0" * 16

    def test_cluster_backend_record_includes_real_trace_span(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query

        _enable_real_telemetry()
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        try:
            _write_one_log(
                tmp_path,
                "mcp-cluster-active-span-1676",
                logs_backend=cluster_backend,
                active_span_name="test.mcp.cluster_active_span",
            )
        finally:
            _reset_spans()

        with _wired_app_module(tmp_path, logs_backend=cluster_backend):
            result = extract_mcp_data(
                handle_admin_logs_query(
                    {"search": "mcp-cluster-active-span-1676"}, _admin_user()
                )
            )

        assert result["success"] is True
        assert len(result["logs"]) == 1
        entry = result["logs"][0]
        assert len(entry["trace_id"]) == 32
        assert len(entry["span_id"]) == 16
        assert entry["trace_id"] != "0" * 32
        assert entry["span_id"] != "0" * 16


class TestAdminLogsExportTraceSpan:
    def test_local_backend_export_includes_zero_trace_span(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import admin_logs_export

        _write_one_log(tmp_path, "mcp-export-local-trace-span-1676")

        with _wired_app_module(tmp_path, logs_backend=None):
            result = extract_mcp_data(
                admin_logs_export(
                    {
                        "format": "json",
                        "search": "mcp-export-local-trace-span-1676",
                    },
                    _admin_user(),
                )
            )

        assert result["success"] is True
        data = json.loads(result["data"])
        assert len(data["logs"]) == 1
        entry = data["logs"][0]
        assert entry["trace_id"] == "0" * 32
        assert entry["span_id"] == "0" * 16

    def test_cluster_backend_export_includes_zero_trace_span(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import admin_logs_export

        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        _write_one_log(
            tmp_path, "mcp-export-trace-span-1676", logs_backend=cluster_backend
        )

        with _wired_app_module(tmp_path, logs_backend=cluster_backend):
            result = extract_mcp_data(
                admin_logs_export(
                    {"format": "json", "search": "mcp-export-trace-span-1676"},
                    _admin_user(),
                )
            )

        assert result["success"] is True
        data = json.loads(result["data"])
        assert len(data["logs"]) == 1
        entry = data["logs"][0]
        assert entry["trace_id"] == "0" * 32
        assert entry["span_id"] == "0" * 16

    def test_cluster_backend_export_includes_real_trace_span(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import admin_logs_export

        _enable_real_telemetry()
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        try:
            _write_one_log(
                tmp_path,
                "mcp-export-active-span-1676",
                logs_backend=cluster_backend,
                active_span_name="test.mcp.export_active_span",
            )
        finally:
            _reset_spans()

        with _wired_app_module(tmp_path, logs_backend=cluster_backend):
            result = extract_mcp_data(
                admin_logs_export(
                    {"format": "json", "search": "mcp-export-active-span-1676"},
                    _admin_user(),
                )
            )

        assert result["success"] is True
        data = json.loads(result["data"])
        assert len(data["logs"]) == 1
        entry = data["logs"][0]
        assert len(entry["trace_id"]) == 32
        assert len(entry["span_id"]) == 16
        assert entry["trace_id"] != "0" * 32
        assert entry["span_id"] != "0" * 16
