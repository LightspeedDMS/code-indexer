"""RED-first tests for Bug #1553: LogAggregatorService cluster-read dispatch.

Root cause: in cluster mode, SQLiteLogHandler's writer thread routes EVERY
log record through the injected LogsBackend (PostgreSQL) once it's wired at
startup (~8-10s in), while nearly every reader (MCP admin_logs_query, REST
/admin/api/logs, the web logs page's standalone branch) constructs
LogAggregatorService(log_db_path) with NO knowledge of that backend --
so it reads the now-frozen, empty node-local logs.db forever. This is a
write-here/read-there split, not "the log store stops capturing".

The fix makes LogAggregatorService itself backend-aware: an optional
`logs_backend` constructor param, defaulting to None (today's exact
behaviour), dispatched internally via an explicit `is_cross_node_backend`
capability check (never a "Postgres" in type(x).__name__ string match).

These tests use REAL LogsSqliteBackend instances -- one representing the
node-local file (SQLite backend, is_cross_node_backend=False, the default),
and one representing a "cluster-like" distinct store: a real LogsSqliteBackend
pointed at a DIFFERENT db file, with is_cross_node_backend overridden to True
via a tiny real subclass. This exercises the identical dispatch code path a
real PostgreSQL backend would (same Protocol, same query_logs contract) --
only the capability flag is flipped for the test, never the database
behaviour itself. No mocks.
"""

from __future__ import annotations

import logging

from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend


class _FakeClusterBackend(LogsSqliteBackend):
    """A REAL SQLite-backed LogsBackend, tagged as cross-node for tests.

    Simulates "this is a distinct, genuinely cross-node store" (what a real
    LogsPostgresBackend would declare) without requiring a live PostgreSQL
    connection. All read/write behaviour is real SQLite -- only the
    dispatch-relevant capability flag differs from the plain LogsSqliteBackend
    default.
    """

    is_cross_node_backend: bool = True


def _insert_and_flush(handler, *, level: str, message: str) -> None:
    """Emit one real log record through the handler and drain the writer."""
    record = logging.LogRecord(
        name="bug1553.test",
        level=getattr(logging, level),
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    handler.flush()


class TestClusterReadFollowsClusterWrite:
    """(a)/(b): records written after backend injection are readable via
    the aggregator in cluster mode, and carry an `id` for watermark diffing.
    """

    def test_record_written_via_cluster_backend_is_readable_via_aggregator(
        self, tmp_path
    ):
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        # Handler wired exactly like production cluster mode: logs_backend
        # injected, so ALL writes route through the backend, never the
        # local file (sqlite_log_handler.py's _writer_loop routing).
        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(
                handler, level="ERROR", message="cluster-write-marker-1553"
            )
        finally:
            handler.close()

        # The local file must be untouched -- proves the write truly went
        # to the cluster backend, not the node-local path.
        service_no_backend = LogAggregatorService(local_logs_db)
        empty_result = service_no_backend.query(search="cluster-write-marker-1553")
        assert empty_result["logs"] == []

        # The cluster-aware aggregator MUST see it.
        service_with_backend = LogAggregatorService(
            local_logs_db, logs_backend=cluster_backend
        )
        result = service_with_backend.query(search="cluster-write-marker-1553")
        assert len(result["logs"]) == 1
        assert result["logs"][0]["message"] == "cluster-write-marker-1553"
        assert result["logs"][0]["id"] is not None

    def test_is_cluster_mode_reflects_backend_capability(self, tmp_path):
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        assert LogAggregatorService(local_logs_db).is_cluster_mode is False
        assert (
            LogAggregatorService(
                local_logs_db, logs_backend=cluster_backend
            ).is_cluster_mode
            is True
        )

    def test_query_all_through_cluster_backend_returns_written_record(self, tmp_path):
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))

        handler = SQLiteLogHandler(db_path=local_logs_db, logs_backend=cluster_backend)
        try:
            _insert_and_flush(handler, level="WARNING", message="query-all-marker-1553")
        finally:
            handler.close()

        service = LogAggregatorService(local_logs_db, logs_backend=cluster_backend)
        logs = service.query_all(search="query-all-marker-1553")

        assert len(logs) == 1
        assert logs[0]["message"] == "query-all-marker-1553"
        assert logs[0]["id"] is not None


class TestMultiLevelAndSearchThroughClusterBackend:
    """(c): multi-level filtering and text search still work through the
    cluster-backend dispatch path (not silently dropped by routing through
    a plain query_logs() call).
    """

    def _seed(self, handler) -> None:
        _insert_and_flush(handler, level="ERROR", message="disk failure 1553")
        _insert_and_flush(handler, level="WARNING", message="cache stale 1553")
        _insert_and_flush(handler, level="INFO", message="startup ok 1553")

    def test_levels_list_filters_through_backend(self, tmp_path):
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )
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

        service = LogAggregatorService(local_logs_db, logs_backend=cluster_backend)
        result = service.query(levels=["ERROR", "WARNING"], search="1553")

        levels_seen = {entry["level"] for entry in result["logs"]}
        assert levels_seen == {"ERROR", "WARNING"}

    def test_search_filters_through_backend(self, tmp_path):
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )
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

        service = LogAggregatorService(local_logs_db, logs_backend=cluster_backend)
        result = service.query(search="cache stale")

        assert len(result["logs"]) == 1
        assert result["logs"][0]["message"] == "cache stale 1553"


class TestNodeIdFilterThroughClusterBackend:
    """web/routes.py's inline cluster branch supported node_id filtering
    directly against logs_backend.query_logs(node_id=...) -- this must be
    preserved when unified through LogAggregatorService.query().
    """

    def test_node_id_filters_through_backend(self, tmp_path):
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )

        cluster_backend = _FakeClusterBackend(db_path=str(tmp_path / "cluster.db"))
        cluster_backend.insert_log(
            timestamp="2026-01-01T00:00:00Z",
            level="ERROR",
            source="mod.a",
            message="node-a-only",
            node_id="node-a",
        )
        cluster_backend.insert_log(
            timestamp="2026-01-01T00:00:01Z",
            level="ERROR",
            source="mod.a",
            message="node-b-only",
            node_id="node-b",
        )

        service = LogAggregatorService(
            tmp_path / "logs.db", logs_backend=cluster_backend
        )
        result = service.query(node_id="node-a")

        messages = {entry["message"] for entry in result["logs"]}
        assert messages == {"node-a-only"}


class TestSoloModeUnaffected:
    """(d): solo/SQLite mode -- no backend, or a node-local LogsSqliteBackend
    (is_cross_node_backend=False, exactly what solo-mode production wiring
    injects) -- must behave identically: reads/writes stay on the local
    file, never dispatched through the backend object as a distinct store.
    """

    def test_no_backend_write_then_read_round_trips(self, tmp_path):
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        handler = SQLiteLogHandler(db_path=local_logs_db)
        try:
            _insert_and_flush(handler, level="ERROR", message="solo-marker-1553")
        finally:
            handler.close()

        service = LogAggregatorService(local_logs_db)
        result = service.query(search="solo-marker-1553")

        assert len(result["logs"]) == 1
        assert result["logs"][0]["message"] == "solo-marker-1553"

    def test_node_local_backend_injected_still_uses_local_path(self, tmp_path):
        """Production solo-mode wiring: a real LogsSqliteBackend IS injected
        (backend_registry.logs), but since it declares
        is_cross_node_backend=False, the aggregator must still resolve reads
        through the local file path, not treat it as a distinct store.
        """
        from code_indexer.server.services.log_aggregator_service import (
            LogAggregatorService,
        )
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        local_logs_db = tmp_path / "logs.db"
        # Solo-mode production shape: LogsSqliteBackend pointed at the SAME
        # file the handler's direct-SQLite path would use.
        node_local_backend = LogsSqliteBackend(db_path=str(local_logs_db))

        handler = SQLiteLogHandler(
            db_path=local_logs_db, logs_backend=node_local_backend
        )
        try:
            _insert_and_flush(
                handler, level="ERROR", message="solo-backend-marker-1553"
            )
        finally:
            handler.close()

        service = LogAggregatorService(local_logs_db, logs_backend=node_local_backend)
        assert service.is_cluster_mode is False

        result = service.query(search="solo-backend-marker-1553")
        assert len(result["logs"]) == 1
        assert result["logs"][0]["message"] == "solo-backend-marker-1553"
