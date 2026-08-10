"""RED-first tests for Bug #1553: handle_admin_logs_query cluster-mode reads.

THE most important read surface for this bug: handle_admin_logs_query is the
front door the CLAUDE.md-mandated post-E2E log-audit gate
(tests/e2e/log_audit_gate.py) uses to diff new ERROR/WARNING entries by
watermark id. Before this fix, the handler constructed
LogAggregatorService(log_db_path) with NO knowledge of app.state.logs_backend
-- so in cluster mode, once the writer thread's backend is wired at startup
(~8-10s in), the handler reads a permanently frozen, empty node-local
logs.db forever, even though every log line since is safely stored in the
real (PostgreSQL, in production) cross-node backend.

Uses a REAL SQLiteLogHandler and a REAL LogsSqliteBackend subclass tagged as
cross-node (see test_log_aggregator_cluster_dispatch_1553.py's
_FakeClusterBackend rationale) -- no mocking of the read/write pipeline
itself. Only app_module (the module-level app singleton reference) is
patched, per the established MCP handler test pattern
(test_admin_embedding_stats_query_1418.py).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

from .conftest import extract_mcp_data


class _FakeClusterBackend(LogsSqliteBackend):
    """Real SQLite-backed LogsBackend tagged as cross-node for tests.

    See test_log_aggregator_cluster_dispatch_1553.py for the full rationale:
    this exercises the identical LogsBackend Protocol / dispatch code path a
    real LogsPostgresBackend would, without requiring a live PostgreSQL
    connection.
    """

    is_cross_node_backend: bool = True


def _insert_and_flush(handler, *, level: str, message: str) -> None:
    record = logging.LogRecord(
        name="bug1553.mcp",
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
        username="admin-1553",
        role=UserRole.ADMIN,
        password_hash="x",
        created_at=datetime.now(timezone.utc),
    )


class TestAdminLogsQueryClusterRead:
    def test_cluster_backend_record_is_readable_via_mcp_handler(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(handler, level="ERROR", message="mcp-cluster-marker-1553")
        finally:
            handler.close()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.log_db_path = str(local_logs_db)
            mock_app_module.app.state.logs_backend = cluster_backend

            result = extract_mcp_data(
                handle_admin_logs_query(
                    {"search": "mcp-cluster-marker-1553"}, _admin_user()
                )
            )

        assert result["success"] is True
        assert len(result["logs"]) == 1
        entry = result["logs"][0]
        assert entry["message"] == "mcp-cluster-marker-1553"
        assert entry["id"] is not None

    def test_watermark_diff_via_filter_new_entries_works(self, tmp_path):
        """(b): returned dicts carry `id` and the watermark diff the audit
        gate relies on (filter_new_entries) correctly separates pre- and
        post-watermark entries read through the cluster backend.
        """
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from tests.e2e.log_audit_gate import filter_new_entries

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(handler, level="ERROR", message="watermark-before-1553")
            _insert_and_flush(handler, level="ERROR", message="watermark-after-1553")
        finally:
            handler.close()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.log_db_path = str(local_logs_db)
            mock_app_module.app.state.logs_backend = cluster_backend

            result = extract_mcp_data(
                handle_admin_logs_query({"search": "watermark-"}, _admin_user())
            )

        logs = result["logs"]
        assert len(logs) == 2
        by_message = {entry["message"]: entry for entry in logs}
        before_id = by_message["watermark-before-1553"]["id"]
        after_id = by_message["watermark-after-1553"]["id"]
        assert after_id > before_id

        new_entries = filter_new_entries(logs, watermark_id=before_id)
        new_messages = {entry["message"] for entry in new_entries}
        assert new_messages == {"watermark-after-1553"}

    def test_negative_control_without_backend_wiring_sees_nothing(self, tmp_path):
        """Sanity control: proves the previous test's success genuinely
        depends on logs_backend being wired on app.state, not on shared
        file-path coincidence.
        """
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(handler, level="ERROR", message="unwired-marker-1553")
        finally:
            handler.close()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.log_db_path = str(local_logs_db)
            mock_app_module.app.state.logs_backend = None

            result = extract_mcp_data(
                handle_admin_logs_query(
                    {"search": "unwired-marker-1553"}, _admin_user()
                )
            )

        assert result["success"] is True
        assert result["logs"] == []


class TestAdminLogsExportClusterRead:
    def test_cluster_backend_record_is_exportable_via_mcp_handler(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import admin_logs_export
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(handler, level="ERROR", message="export-marker-1553")
        finally:
            handler.close()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.log_db_path = str(local_logs_db)
            mock_app_module.app.state.logs_backend = cluster_backend

            result = extract_mcp_data(
                admin_logs_export(
                    {"format": "json", "search": "export-marker-1553"},
                    _admin_user(),
                )
            )

        assert result["success"] is True
        assert result["count"] == 1
        assert "export-marker-1553" in result["data"]


class TestAdminLogsQueryClusterFiltering:
    def _seed(self, handler) -> None:
        _insert_and_flush(handler, level="ERROR", message="mcp-disk-1553")
        _insert_and_flush(handler, level="WARNING", message="mcp-cache-1553")
        _insert_and_flush(handler, level="INFO", message="mcp-startup-1553")

    def test_multi_level_filter_through_mcp_handler(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            self._seed(handler)
        finally:
            handler.close()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.log_db_path = str(local_logs_db)
            mock_app_module.app.state.logs_backend = cluster_backend

            result = extract_mcp_data(
                handle_admin_logs_query(
                    {"level": "ERROR,WARNING", "search": "mcp-"}, _admin_user()
                )
            )

        levels_seen = {entry["level"] for entry in result["logs"]}
        assert levels_seen == {"ERROR", "WARNING"}

    def test_search_through_mcp_handler(self, tmp_path):
        from code_indexer.server.mcp.handlers.admin import handle_admin_logs_query
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            self._seed(handler)
        finally:
            handler.close()

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module"
        ) as mock_app_module:
            mock_app_module.app.state.log_db_path = str(local_logs_db)
            mock_app_module.app.state.logs_backend = cluster_backend

            result = extract_mcp_data(
                handle_admin_logs_query({"search": "mcp-cache"}, _admin_user())
            )

        assert len(result["logs"]) == 1
        assert result["logs"][0]["message"] == "mcp-cache-1553"


# (Bug #1553 note: TestAdminLogsExportClusterRead is defined once, above,
# right after TestAdminLogsQueryClusterRead -- a duplicate definition that
# used to live here was removed because Python class redefinition silently
# shadows the earlier one, so pytest was only collecting the duplicate.)
