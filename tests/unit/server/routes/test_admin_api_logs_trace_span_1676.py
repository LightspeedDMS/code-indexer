"""Story #1676 AC2: REST /admin/api/logs must expose trace_id/span_id.

Mirrors test_admin_api_logs_cluster_read_1553.py's exact pattern (real
SQLiteLogHandler write path, endpoints called directly as plain functions
with a minimal fake Request) but specifically for the cluster-mode
backend-dispatched read path -- the "local-only fix, cluster mode silently
untouched" trap this story calls out explicitly.
"""

from __future__ import annotations

import logging
from typing import Any

from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend


class _FakeClusterBackend(LogsSqliteBackend):
    """Real SQLite-backed LogsBackend tagged as cross-node for tests."""

    is_cross_node_backend: bool = True


class _FakeAppState:
    log_db_path: str = ""
    logs_backend: Any = None


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeAppState()


class _FakeRequest:
    def __init__(self) -> None:
        self.app = _FakeApp()


def _admin_user():
    from datetime import datetime, timezone

    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin-1676-rest",
        role=UserRole.ADMIN,
        password_hash="x",
        created_at=datetime.now(timezone.utc),
    )


def _write_log_line(handler, message: str, level: str = "ERROR") -> None:
    record = logging.LogRecord(
        name="test.admin_api.trace_span",
        level=getattr(logging, level),
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    handler.flush()


class TestGetLogsExposesTraceSpanLocalPath:
    """Solo-mode (local/direct-SQLite) path: no logs_backend wired."""

    def test_get_logs_includes_zero_trace_span_when_no_active_span(
        self, tmp_path
    ) -> None:
        from code_indexer.server.routes.admin_api import get_logs
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        local_logs_db = tmp_path / "logs.db"
        handler = SQLiteLogHandler(db_path=local_logs_db)
        try:
            _write_log_line(handler, "rest-local-no-span-1676")
        finally:
            handler.close()

        request = _FakeRequest()
        request.app.state.log_db_path = str(local_logs_db)
        request.app.state.logs_backend = None

        response = get_logs(
            request=request,
            search="rest-local-no-span-1676",
            user=_admin_user(),
        )

        assert len(response.logs) == 1
        assert response.logs[0].trace_id == "0" * 32
        assert response.logs[0].span_id == "0" * 16


class TestGetLogsExposesTraceSpanClusterPath:
    """Cluster-mode backend-dispatched read path: logs_backend wired and
    declares is_cross_node_backend=True, exactly as PostgreSQL cluster mode
    does. This is the path Bug #1553 fixed dispatch for, and the path
    #1653/#1654/#1662-style bugs left silently broken by local-only fixes."""

    def test_get_logs_includes_zero_trace_span_via_cluster_backend(
        self, tmp_path
    ) -> None:
        from code_indexer.server.routes.admin_api import get_logs
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _write_log_line(handler, "rest-cluster-no-span-1676")
        finally:
            handler.close()

        request = _FakeRequest()
        request.app.state.log_db_path = str(local_logs_db)
        request.app.state.logs_backend = cluster_backend

        response = get_logs(
            request=request,
            search="rest-cluster-no-span-1676",
            user=_admin_user(),
        )

        assert len(response.logs) == 1
        assert response.logs[0].trace_id == "0" * 32
        assert response.logs[0].span_id == "0" * 16

    def test_get_logs_includes_real_trace_span_via_cluster_backend(
        self, tmp_path
    ) -> None:
        from code_indexer.server.routes.admin_api import get_logs
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.telemetry import get_telemetry_manager
        from code_indexer.server.telemetry.spans import create_span, reset_spans_state
        from code_indexer.server.utils.config_manager import TelemetryConfig

        config = TelemetryConfig(enabled=True, export_traces=True)
        get_telemetry_manager(config)

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            with create_span("test.admin_api.cluster_active_span"):
                _write_log_line(handler, "rest-cluster-active-span-1676")
        finally:
            handler.close()
            reset_spans_state()

        request = _FakeRequest()
        request.app.state.log_db_path = str(local_logs_db)
        request.app.state.logs_backend = cluster_backend

        response = get_logs(
            request=request,
            search="rest-cluster-active-span-1676",
            user=_admin_user(),
        )

        assert len(response.logs) == 1
        entry = response.logs[0]
        assert entry.trace_id is not None and len(entry.trace_id) == 32
        assert entry.span_id is not None and len(entry.span_id) == 16
        assert entry.trace_id != "0" * 32
        assert entry.span_id != "0" * 16
