"""RED-first tests for Bug #1553: REST /admin/api/logs cluster-mode reads.

routes/admin_api.py's get_logs (GET /admin/api/logs) and export_logs
(GET /admin/api/logs/export) each construct LogAggregatorService(log_db_path)
with no knowledge of request.app.state.logs_backend -- so in cluster mode
both endpoints always read the frozen, empty node-local logs.db once the
writer thread's backend is wired at startup.

Endpoints are called DIRECTLY as plain functions (bypassing FastAPI's DI/ASGI
pipeline entirely) with a minimal fake Request exposing only `.app.state`,
since that is the only attribute either endpoint reads from `request`. Real
SQLiteLogHandler + a real LogsSqliteBackend subclass tagged as cross-node
(see test_log_aggregator_cluster_dispatch_1553.py's _FakeClusterBackend
rationale) exercise the real read/write pipeline -- no mocking.
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
    """Minimal stand-in for fastapi.Request -- both endpoints under test
    read ONLY request.app.state, never anything else off the real Request.
    """

    def __init__(self) -> None:
        self.app = _FakeApp()


def _insert_and_flush(handler, *, level: str, message: str) -> None:
    record = logging.LogRecord(
        name="bug1553.admin_api",
        level=getattr(logging, level),
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    handler.flush()


def _admin_user():
    from datetime import datetime, timezone

    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin-1553-rest",
        role=UserRole.ADMIN,
        password_hash="x",
        created_at=datetime.now(timezone.utc),
    )


class TestGetLogsClusterRead:
    def test_cluster_backend_record_is_readable_via_get_logs(self, tmp_path):
        from code_indexer.server.routes.admin_api import get_logs
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(
                handler, level="ERROR", message="rest-cluster-marker-1553"
            )
        finally:
            handler.close()

        request = _FakeRequest()
        request.app.state.log_db_path = str(local_logs_db)
        request.app.state.logs_backend = cluster_backend

        response = get_logs(
            request=request,
            search="rest-cluster-marker-1553",
            user=_admin_user(),
        )

        assert len(response.logs) == 1
        assert response.logs[0].message == "rest-cluster-marker-1553"

    def test_negative_control_without_backend_wiring_sees_nothing(self, tmp_path):
        from code_indexer.server.routes.admin_api import get_logs
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(
                handler, level="ERROR", message="rest-unwired-marker-1553"
            )
        finally:
            handler.close()

        request = _FakeRequest()
        request.app.state.log_db_path = str(local_logs_db)
        request.app.state.logs_backend = None

        response = get_logs(
            request=request,
            search="rest-unwired-marker-1553",
            user=_admin_user(),
        )

        assert response.logs == []


class TestExportLogsClusterRead:
    def test_cluster_backend_record_is_readable_via_export_logs(self, tmp_path):
        from code_indexer.server.routes.admin_api import export_logs
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(handler, level="ERROR", message="rest-export-marker-1553")
        finally:
            handler.close()

        request = _FakeRequest()
        request.app.state.log_db_path = str(local_logs_db)
        request.app.state.logs_backend = cluster_backend

        response = export_logs(
            request=request,
            format="json",
            search="rest-export-marker-1553",
            user=_admin_user(),
        )

        # Parse the actual JSON body -- a raw substring check would pass
        # even with zero matching rows, since the formatter always echoes
        # the `search` filter value into metadata.filters regardless of
        # whether any log rows were found.
        import json

        body = json.loads(response.body.decode("utf-8"))
        assert body["metadata"]["count"] == 1
        assert len(body["logs"]) == 1
        assert body["logs"][0]["message"] == "rest-export-marker-1553"
