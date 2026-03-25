"""
Tests for LogsBackend Protocol and LogsSqliteBackend implementation (Story #500).

TDD approach: tests written BEFORE implementation.

Covers:
- AC1: LogsBackend Protocol is runtime-checkable
- AC2: LogsSqliteBackend satisfies Protocol and implements all methods correctly
- AC3: BackendRegistry has logs field; StorageFactory creates it in SQLite mode
"""

import os
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# AC1: Protocol
# ---------------------------------------------------------------------------


class TestLogsBackendProtocol:
    """Tests for the LogsBackend Protocol definition (AC1)."""

    def test_logs_backend_protocol_is_runtime_checkable(self):
        """LogsBackend must be decorated with @runtime_checkable."""
        from code_indexer.server.storage.protocols import LogsBackend

        # A protocol decorated with @runtime_checkable allows isinstance() checks.
        # We verify this by checking that it is indeed a Protocol class that
        # can be used for isinstance checks (won't raise TypeError).
        assert hasattr(LogsBackend, "__protocol_attrs__") or hasattr(
            LogsBackend, "_is_protocol"
        ), "LogsBackend must be a Protocol"

        # Verify isinstance check works (does not raise TypeError).
        # Use a dummy object that doesn't satisfy the protocol.
        class NotABackend:
            pass

        # Should not raise TypeError (which would happen for non-runtime_checkable protocols)
        try:
            isinstance(NotABackend(), LogsBackend)
            # It either returns True or False without raising
        except TypeError:
            pytest.fail(
                "isinstance() raised TypeError — LogsBackend is not @runtime_checkable"
            )

    def test_logs_backend_protocol_has_required_methods(self):
        """LogsBackend Protocol must declare all required methods."""
        from code_indexer.server.storage.protocols import LogsBackend

        protocol_methods = dir(LogsBackend)

        assert "insert_log" in protocol_methods, "LogsBackend must have insert_log()"
        assert "query_logs" in protocol_methods, "LogsBackend must have query_logs()"
        assert (
            "cleanup_old_logs" in protocol_methods
        ), "LogsBackend must have cleanup_old_logs()"
        assert "close" in protocol_methods, "LogsBackend must have close()"


# ---------------------------------------------------------------------------
# AC2: SQLite Backend Implementation
# ---------------------------------------------------------------------------


class TestLogsSqliteBackend:
    """Tests for LogsSqliteBackend implementation (AC2)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Provide a temp directory path for the test database."""
        return str(tmp_path / "test_logs.db")

    @pytest.fixture
    def backend(self, db_path):
        """Create a fresh LogsSqliteBackend for each test."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        b = LogsSqliteBackend(db_path)
        yield b
        b.close()

    def test_logs_sqlite_backend_satisfies_protocol(self, db_path):
        """isinstance(LogsSqliteBackend(...), LogsBackend) must be True."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend
        from code_indexer.server.storage.protocols import LogsBackend

        backend = LogsSqliteBackend(db_path)
        assert isinstance(backend, LogsBackend), (
            "LogsSqliteBackend must satisfy the LogsBackend Protocol. "
            "Check that all protocol methods are implemented with matching signatures."
        )
        backend.close()

    def test_insert_log_writes_to_db(self, backend, db_path):
        """insert_log() must persist a record that can be retrieved."""
        ts = datetime.now(timezone.utc).isoformat()
        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="test.source",
            message="Hello world",
            correlation_id="corr-123",
            user_id="user-1",
            request_path="/api/test",
            extra_data='{"key": "value"}',
            node_id=None,
        )

        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=10,
            offset=0,
        )

        assert total == 1
        assert len(results) == 1
        row = results[0]
        assert row["level"] == "INFO"
        assert row["source"] == "test.source"
        assert row["message"] == "Hello world"
        assert row["correlation_id"] == "corr-123"
        assert row["user_id"] == "user-1"
        assert row["request_path"] == "/api/test"

    def test_insert_log_with_node_id(self, backend):
        """insert_log() must store node_id when provided."""
        ts = datetime.now(timezone.utc).isoformat()
        backend.insert_log(
            timestamp=ts,
            level="DEBUG",
            source="cluster.node",
            message="Node heartbeat",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id="node-42",
        )

        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id="node-42",
            limit=10,
            offset=0,
        )

        assert total == 1
        row = results[0]
        assert row["node_id"] == "node-42"

    def test_query_logs_returns_filtered_results_with_pagination(self, backend):
        """query_logs() must return (list, total_count) and support pagination."""
        ts = datetime.now(timezone.utc).isoformat()

        # Insert 5 records
        for i in range(5):
            backend.insert_log(
                timestamp=ts,
                level="INFO",
                source="test.pagination",
                message=f"Message {i}",
                correlation_id=None,
                user_id=None,
                request_path=None,
                extra_data=None,
                node_id=None,
            )

        # Page 1: limit=3, offset=0
        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=3,
            offset=0,
        )
        assert total == 5, f"Expected total=5 but got {total}"
        assert len(results) == 3

        # Page 2: limit=3, offset=3
        results2, total2 = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=3,
            offset=3,
        )
        assert total2 == 5
        assert len(results2) == 2

    def test_query_logs_filters_by_level(self, backend):
        """query_logs(level=...) must return only records matching that level."""
        ts = datetime.now(timezone.utc).isoformat()

        backend.insert_log(
            timestamp=ts,
            level="ERROR",
            source="svc",
            message="Error msg",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )
        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="svc",
            message="Info msg",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )
        backend.insert_log(
            timestamp=ts,
            level="WARNING",
            source="svc",
            message="Warning msg",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )

        results, total = backend.query_logs(
            level="ERROR",
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=100,
            offset=0,
        )

        assert total == 1
        assert len(results) == 1
        assert results[0]["level"] == "ERROR"

    def test_query_logs_filters_by_source(self, backend):
        """query_logs(source=...) must return only records matching that source."""
        ts = datetime.now(timezone.utc).isoformat()

        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="auth.service",
            message="Auth event",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )
        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="repo.manager",
            message="Repo event",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )

        results, total = backend.query_logs(
            level=None,
            source="auth.service",
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=100,
            offset=0,
        )

        assert total == 1
        assert results[0]["source"] == "auth.service"

    def test_query_logs_filters_by_node_id(self, backend):
        """query_logs(node_id=...) must return only records for that node."""
        ts = datetime.now(timezone.utc).isoformat()

        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="svc",
            message="From node A",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id="node-A",
        )
        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="svc",
            message="From node B",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id="node-B",
        )
        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="svc",
            message="No node",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )

        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id="node-A",
            limit=100,
            offset=0,
        )

        assert total == 1
        assert results[0]["node_id"] == "node-A"
        assert results[0]["message"] == "From node A"

    def test_query_logs_filters_by_date_range(self, backend):
        """query_logs(date_from, date_to) must filter records by timestamp."""
        now = datetime.now(timezone.utc)

        # Three records: yesterday, today, tomorrow
        yesterday = (now - timedelta(days=1)).isoformat()
        today = now.isoformat()
        tomorrow = (now + timedelta(days=1)).isoformat()

        for ts, msg in [
            (yesterday, "Yesterday"),
            (today, "Today"),
            (tomorrow, "Tomorrow"),
        ]:
            backend.insert_log(
                timestamp=ts,
                level="INFO",
                source="svc",
                message=msg,
                correlation_id=None,
                user_id=None,
                request_path=None,
                extra_data=None,
                node_id=None,
            )

        # Query: only today and tomorrow (date_from = 1 hour ago from now)
        date_from = (now - timedelta(hours=1)).isoformat()
        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=date_from,
            date_to=None,
            node_id=None,
            limit=100,
            offset=0,
        )

        assert total == 2
        messages = {r["message"] for r in results}
        assert "Today" in messages
        assert "Tomorrow" in messages
        assert "Yesterday" not in messages

    def test_query_logs_filters_by_correlation_id(self, backend):
        """query_logs(correlation_id=...) must return only matching records."""
        ts = datetime.now(timezone.utc).isoformat()

        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="svc",
            message="Correlated",
            correlation_id="req-abc",
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )
        backend.insert_log(
            timestamp=ts,
            level="INFO",
            source="svc",
            message="Different correlation",
            correlation_id="req-xyz",
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )

        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id="req-abc",
            date_from=None,
            date_to=None,
            node_id=None,
            limit=100,
            offset=0,
        )

        assert total == 1
        assert results[0]["correlation_id"] == "req-abc"

    def test_cleanup_old_logs_removes_old_records(self, backend):
        """cleanup_old_logs(days) must delete records older than days_to_keep."""
        now = datetime.now(timezone.utc)

        # Insert old record (40 days ago)
        old_ts = (now - timedelta(days=40)).isoformat()
        backend.insert_log(
            timestamp=old_ts,
            level="INFO",
            source="svc",
            message="Old log",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )

        # Insert recent record (1 day ago)
        recent_ts = (now - timedelta(days=1)).isoformat()
        backend.insert_log(
            timestamp=recent_ts,
            level="INFO",
            source="svc",
            message="Recent log",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )

        # Cleanup records older than 30 days
        deleted_count = backend.cleanup_old_logs(days_to_keep=30)

        assert deleted_count == 1, f"Expected 1 deleted record, got {deleted_count}"

        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=100,
            offset=0,
        )
        assert total == 1
        assert results[0]["message"] == "Recent log"

    def test_close_is_a_noop_or_delegating(self, db_path):
        """close() must not raise any exception."""
        from code_indexer.server.storage.sqlite_backends import LogsSqliteBackend

        backend = LogsSqliteBackend(db_path)
        # Should not raise
        backend.close()

    def test_query_logs_returns_dict_with_required_fields(self, backend):
        """Each log dict returned by query_logs must have required fields."""
        ts = datetime.now(timezone.utc).isoformat()
        backend.insert_log(
            timestamp=ts,
            level="ERROR",
            source="test.module",
            message="Test error",
            correlation_id="corr-1",
            user_id="user-1",
            request_path="/path",
            extra_data=None,
            node_id="node-1",
        )

        results, total = backend.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=10,
            offset=0,
        )

        assert total == 1
        row = results[0]
        required_fields = {
            "id",
            "timestamp",
            "level",
            "source",
            "message",
            "correlation_id",
            "user_id",
            "request_path",
            "node_id",
        }
        missing = required_fields - set(row.keys())
        assert not missing, f"Missing fields in log dict: {missing}"


# ---------------------------------------------------------------------------
# AC3: BackendRegistry and StorageFactory
# ---------------------------------------------------------------------------


class TestBackendRegistryLogsField:
    """Tests for BackendRegistry.logs field (AC3)."""

    def test_backend_registry_has_logs_field(self):
        """BackendRegistry dataclass must have a 'logs' field typed as LogsBackend."""
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "logs" in fields, (
            "BackendRegistry must have a 'logs' field. " f"Current fields: {fields}"
        )

    def test_storage_factory_creates_logs_backend_sqlite_mode(self, tmp_path):
        """StorageFactory._create_sqlite_backends must produce a valid LogsBackend."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import LogsBackend

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)

        assert hasattr(registry, "logs"), "BackendRegistry must have .logs attribute"
        assert isinstance(
            registry.logs, LogsBackend
        ), f"registry.logs must satisfy LogsBackend protocol, got {type(registry.logs)}"

    def test_storage_factory_logs_backend_is_functional(self, tmp_path):
        """The logs backend from StorageFactory must be able to insert and query logs."""
        from code_indexer.server.storage.factory import StorageFactory

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)
        logs = registry.logs

        ts = datetime.now(timezone.utc).isoformat()
        logs.insert_log(
            timestamp=ts,
            level="INFO",
            source="factory.test",
            message="Factory test log",
            correlation_id=None,
            user_id=None,
            request_path=None,
            extra_data=None,
            node_id=None,
        )

        results, total = logs.query_logs(
            level=None,
            source=None,
            correlation_id=None,
            date_from=None,
            date_to=None,
            node_id=None,
            limit=10,
            offset=0,
        )

        assert total == 1
        assert results[0]["message"] == "Factory test log"
