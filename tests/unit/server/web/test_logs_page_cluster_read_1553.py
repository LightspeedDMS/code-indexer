"""RED-first tests for Bug #1553: Web UI logs routes cluster-mode reads.

logs_page/logs_list_partial (web/routes.py) already had a hand-rolled
if/else branch that reads directly from logs_backend.query_logs() in
"cluster mode" (detected via a fragile "Postgres" in type(x).__name__
string match) -- but export_logs_web has NO such branch at all, so it
ALWAYS reads the (in cluster mode, frozen/empty) node-local logs.db file
regardless of backend. This file proves the TARGET behaviour: a record
written through a cluster-mode backend is visible in all three routes once
app.state.logs_backend is wired, and that cluster mode's node_id filtering
capability (already present in the pre-fix inline branch) survives the
refactor onto the unified LogAggregatorService dispatch.

Route functions are called DIRECTLY (bypassing FastAPI's DI/ASGI pipeline,
session manager, and CSRF machinery entirely -- none of that is under test
here) with a minimal fake Request exposing only `.app.state`, and
_require_admin_session patched to a fixed mock session. Real
SQLiteLogHandler + a real LogsSqliteBackend subclass tagged as cross-node
exercise the real read/write pipeline -- no mocking of that.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from starlette.status import HTTP_200_OK

# Module-level side effect: importing the real app singleton triggers
# init_session_manager() (routers/inline_routes.py), which set_csrf_cookie
# requires even when logs_page/logs_list_partial are called directly
# rather than through the real app's ASGI pipeline.
from code_indexer.server.app import app as _app_singleton  # noqa: F401
from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

# Arbitrary placeholder line number for synthetic LogRecord construction --
# this test never inspects lineno, only the message/level fields.
_TEST_LOG_LINE_NUMBER = 1


class _FakeClusterBackend(LogsSqliteBackend):
    """Real SQLite-backed LogsBackend tagged as cross-node for tests."""

    is_cross_node_backend: bool = True


class _FakeRequest:
    """Minimal stand-in for fastapi.Request -- the routes under test only
    read request.app.state plus a few cookie/header lookups that already
    tolerate absence.
    """

    def __init__(self, log_db_path: str, logs_backend) -> None:
        state = SimpleNamespace(log_db_path=log_db_path, logs_backend=logs_backend)
        self.app = SimpleNamespace(state=state)
        self.cookies: dict = {}
        self.headers: dict = {}


def _insert_and_flush(handler, *, level: str, message: str) -> None:
    record = logging.LogRecord(
        name="bug1553.web",
        level=getattr(logging, level),
        pathname=__file__,
        lineno=_TEST_LOG_LINE_NUMBER,
        msg=message,
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    handler.flush()


def _admin_session():
    session = MagicMock()
    session.username = "admin-1553"
    return session


def _patched_auth():
    return patch(
        "code_indexer.server.web.routes._require_admin_session",
        return_value=_admin_session(),
    )


class TestLogsPageClusterRead:
    def test_logs_page_shows_cluster_backend_record(self, tmp_path):
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.web.routes import logs_page

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        try:
            handler = SQLiteLogHandler(
                db_path=local_logs_db, logs_backend=cluster_backend
            )
            try:
                _insert_and_flush(
                    handler, level="ERROR", message="web-logs-page-marker-1553"
                )
            finally:
                handler.close()

            request = _FakeRequest(str(local_logs_db), cluster_backend)
            with _patched_auth():
                response = logs_page(request, search="web-logs-page-marker-1553")

            assert response.status_code == HTTP_200_OK
            assert "web-logs-page-marker-1553" in response.body.decode("utf-8")
        finally:
            cluster_backend.close()

    def test_node_id_filter_reaches_cluster_backend(self, tmp_path):
        """Cluster mode's node_id filter must survive the refactor onto the
        unified LogAggregatorService dispatch (it was present in the
        pre-fix inline cluster branch)."""
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.web.routes import logs_page

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        try:
            handler_a = SQLiteLogHandler(
                db_path=local_logs_db, logs_backend=cluster_backend
            )
            handler_a.set_node_id("node-a")
            handler_b = SQLiteLogHandler(
                db_path=local_logs_db, logs_backend=cluster_backend
            )
            handler_b.set_node_id("node-b")
            try:
                _insert_and_flush(
                    handler_a, level="ERROR", message="node-a-marker-1553"
                )
                _insert_and_flush(
                    handler_b, level="ERROR", message="node-b-marker-1553"
                )
            finally:
                handler_a.close()
                handler_b.close()

            request = _FakeRequest(str(local_logs_db), cluster_backend)
            with _patched_auth():
                response = logs_page(request, node_id="node-a")

            body = response.body.decode("utf-8")
            assert "node-a-marker-1553" in body
            assert "node-b-marker-1553" not in body
        finally:
            cluster_backend.close()

    def test_logs_list_partial_shows_cluster_backend_record(self, tmp_path):
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.web.routes import logs_list_partial

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        try:
            handler = SQLiteLogHandler(
                db_path=local_logs_db, logs_backend=cluster_backend
            )
            try:
                _insert_and_flush(
                    handler, level="ERROR", message="web-partial-marker-1553"
                )
            finally:
                handler.close()

            request = _FakeRequest(str(local_logs_db), cluster_backend)
            with _patched_auth():
                response = logs_list_partial(request, search="web-partial-marker-1553")

            assert response.status_code == HTTP_200_OK
            assert "web-partial-marker-1553" in response.body.decode("utf-8")
        finally:
            cluster_backend.close()


class TestExportLogsWebClusterRead:
    def test_export_logs_web_includes_cluster_backend_record(self, tmp_path):
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.web.routes import export_logs_web

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        try:
            handler = SQLiteLogHandler(
                db_path=local_logs_db, logs_backend=cluster_backend
            )
            try:
                _insert_and_flush(
                    handler, level="ERROR", message="web-export-marker-1553"
                )
            finally:
                handler.close()

            request = _FakeRequest(str(local_logs_db), cluster_backend)
            with _patched_auth():
                response = export_logs_web(
                    request, format="json", search="web-export-marker-1553"
                )

            assert response.status_code == HTTP_200_OK
            # Parse the actual JSON body -- a raw substring check would
            # pass even with zero matching rows, since the formatter
            # always echoes the `search` filter value into metadata
            # regardless of whether any log rows were found.
            body = json.loads(response.body.decode("utf-8"))
            assert body["metadata"]["count"] == 1
            assert len(body["logs"]) == 1
            assert body["logs"][0]["message"] == "web-export-marker-1553"
        finally:
            cluster_backend.close()
