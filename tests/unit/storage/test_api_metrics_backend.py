"""
Tests for ApiMetricsBackend Protocol and ApiMetricsSqliteBackend implementation (Story #502).

TDD approach: tests written BEFORE implementation.

Covers:
- AC1: ApiMetricsBackend Protocol is runtime-checkable with correct method signatures
- AC2: ApiMetricsSqliteBackend satisfies Protocol and implements all methods correctly
- AC3: BackendRegistry has api_metrics field; StorageFactory creates it in SQLite mode
- AC4: Node-ID filtering in get_metrics
- AC5: PostgreSQL backend satisfies the protocol (structural check only, no live DB)
"""

import os
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# AC1: Protocol
# ---------------------------------------------------------------------------


class TestApiMetricsBackendProtocol:
    """Tests for the ApiMetricsBackend Protocol definition (AC1)."""

    def test_api_metrics_backend_protocol_is_runtime_checkable(self):
        """ApiMetricsBackend must be decorated with @runtime_checkable."""
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        # A protocol decorated with @runtime_checkable allows isinstance() checks.
        assert hasattr(ApiMetricsBackend, "__protocol_attrs__") or hasattr(
            ApiMetricsBackend, "_is_protocol"
        ), "ApiMetricsBackend must be a Protocol"

        # Verify isinstance check works (does not raise TypeError).
        class NotABackend:
            pass

        try:
            isinstance(NotABackend(), ApiMetricsBackend)
        except TypeError:
            pytest.fail(
                "isinstance() raised TypeError — ApiMetricsBackend is not @runtime_checkable"
            )

    def test_api_metrics_backend_protocol_has_required_methods(self):
        """ApiMetricsBackend Protocol must declare all required methods."""
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        protocol_methods = dir(ApiMetricsBackend)

        assert "insert_metric" in protocol_methods, (
            "ApiMetricsBackend must have insert_metric()"
        )
        assert "get_metrics" in protocol_methods, (
            "ApiMetricsBackend must have get_metrics()"
        )
        assert "cleanup_old" in protocol_methods, (
            "ApiMetricsBackend must have cleanup_old()"
        )
        assert "reset" in protocol_methods, "ApiMetricsBackend must have reset()"
        assert "close" in protocol_methods, "ApiMetricsBackend must have close()"


# ---------------------------------------------------------------------------
# AC2: SQLite Backend Implementation
# ---------------------------------------------------------------------------


class TestApiMetricsSqliteBackend:
    """Tests for ApiMetricsSqliteBackend implementation (AC2)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Provide a temp directory path for the test database."""
        return str(tmp_path / "test_api_metrics.db")

    @pytest.fixture
    def backend(self, db_path):
        """Create a fresh ApiMetricsSqliteBackend for each test."""
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend

        b = ApiMetricsSqliteBackend(db_path)
        yield b
        b.close()

    def test_api_metrics_sqlite_backend_satisfies_protocol(self, db_path):
        """isinstance(ApiMetricsSqliteBackend(...), ApiMetricsBackend) must be True."""
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        backend = ApiMetricsSqliteBackend(db_path)
        assert isinstance(backend, ApiMetricsBackend), (
            "ApiMetricsSqliteBackend must satisfy the ApiMetricsBackend Protocol. "
            "Check that all protocol methods are implemented with matching signatures."
        )
        backend.close()

    def test_insert_metric_semantic(self, backend):
        """insert_metric('semantic') must record a metric retrievable by get_metrics."""
        backend.insert_metric("semantic")
        metrics = backend.get_metrics(window_seconds=3600)
        assert metrics["semantic_searches"] == 1
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0

    def test_insert_metric_other_index(self, backend):
        """insert_metric('other_index') must record and count correctly."""
        backend.insert_metric("other_index")
        backend.insert_metric("other_index")
        metrics = backend.get_metrics(window_seconds=3600)
        assert metrics["other_index_searches"] == 2
        assert metrics["semantic_searches"] == 0

    def test_insert_metric_regex(self, backend):
        """insert_metric('regex') must record and count correctly."""
        backend.insert_metric("regex")
        metrics = backend.get_metrics(window_seconds=3600)
        assert metrics["regex_searches"] == 1

    def test_insert_metric_other_api(self, backend):
        """insert_metric('other_api') must record and count correctly."""
        backend.insert_metric("other_api")
        backend.insert_metric("other_api")
        backend.insert_metric("other_api")
        metrics = backend.get_metrics(window_seconds=3600)
        assert metrics["other_api_calls"] == 3

    def test_get_metrics_returns_correct_structure(self, backend):
        """get_metrics() must return dict with exactly the 4 expected keys."""
        metrics = backend.get_metrics(window_seconds=60)
        required_keys = {
            "semantic_searches",
            "other_index_searches",
            "regex_searches",
            "other_api_calls",
        }
        assert set(metrics.keys()) == required_keys, (
            f"get_metrics() must return exactly {required_keys}, got {set(metrics.keys())}"
        )

    def test_get_metrics_returns_zeros_when_empty(self, backend):
        """get_metrics() must return zeros when no metrics recorded."""
        metrics = backend.get_metrics(window_seconds=3600)
        assert metrics == {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "other_api_calls": 0,
        }

    def test_get_metrics_window_excludes_old_records(self, backend):
        """get_metrics(window_seconds=...) must exclude records outside the window."""
        # Insert a metric with an old timestamp directly to simulate past records
        import sqlite3
        from datetime import datetime, timezone, timedelta

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn = sqlite3.connect(backend._db_path)
        conn.execute(
            "INSERT INTO api_metrics (metric_type, timestamp) VALUES (?, ?)",
            ("semantic", old_ts),
        )
        conn.commit()
        conn.close()

        # Insert a recent metric via the backend
        backend.insert_metric("semantic")

        # Window of 1 hour (3600 seconds) - should only see the recent one
        metrics = backend.get_metrics(window_seconds=3600)
        assert metrics["semantic_searches"] == 1, (
            f"Expected 1 semantic search in 1-hour window, got {metrics['semantic_searches']}"
        )

    def test_cleanup_old_removes_records_beyond_max_age(self, backend):
        """cleanup_old(max_age_seconds) must delete records older than max_age."""
        import sqlite3
        from datetime import datetime, timezone

        # Insert an old record (3 hours old)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        conn = sqlite3.connect(backend._db_path)
        conn.execute(
            "INSERT INTO api_metrics (metric_type, timestamp) VALUES (?, ?)",
            ("semantic", old_ts),
        )
        conn.commit()
        conn.close()

        # Insert a fresh record
        backend.insert_metric("regex")

        # Cleanup records older than 2 hours (7200 seconds)
        deleted = backend.cleanup_old(max_age_seconds=7200)
        assert deleted == 1, f"Expected 1 deleted record, got {deleted}"

        # Only the recent regex record should remain
        metrics = backend.get_metrics(window_seconds=86400)
        assert metrics["semantic_searches"] == 0
        assert metrics["regex_searches"] == 1

    def test_reset_clears_all_records(self, backend):
        """reset() must delete all records from the database."""
        backend.insert_metric("semantic")
        backend.insert_metric("regex")
        backend.insert_metric("other_api")

        metrics_before = backend.get_metrics(window_seconds=3600)
        assert sum(metrics_before.values()) == 3

        backend.reset()

        metrics_after = backend.get_metrics(window_seconds=3600)
        assert metrics_after == {
            "semantic_searches": 0,
            "other_index_searches": 0,
            "regex_searches": 0,
            "other_api_calls": 0,
        }

    def test_close_does_not_raise(self, db_path):
        """close() must not raise any exception."""
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend

        b = ApiMetricsSqliteBackend(db_path)
        b.close()  # Must not raise

    def test_insert_metric_with_explicit_timestamp(self, backend):
        """insert_metric() with explicit timestamp stores that exact timestamp."""
        import sqlite3

        ts = datetime.now(timezone.utc).isoformat()
        backend.insert_metric("semantic", timestamp=ts)

        conn = sqlite3.connect(backend._db_path)
        rows = conn.execute("SELECT metric_type, timestamp FROM api_metrics").fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == "semantic"
        assert rows[0][1] == ts

    def test_insert_metric_with_node_id(self, backend):
        """insert_metric() with node_id stores the node_id column."""
        import sqlite3

        backend.insert_metric("semantic", node_id="node-cluster-1")

        conn = sqlite3.connect(backend._db_path)
        rows = conn.execute("SELECT metric_type, node_id FROM api_metrics").fetchall()
        conn.close()

        assert len(rows) == 1
        assert rows[0][0] == "semantic"
        assert rows[0][1] == "node-cluster-1"

    def test_multiple_metric_types_counted_independently(self, backend):
        """Each metric type must be counted independently in get_metrics()."""
        backend.insert_metric("semantic")
        backend.insert_metric("semantic")
        backend.insert_metric("other_index")
        backend.insert_metric("regex")
        backend.insert_metric("other_api")
        backend.insert_metric("other_api")

        metrics = backend.get_metrics(window_seconds=3600)
        assert metrics["semantic_searches"] == 2
        assert metrics["other_index_searches"] == 1
        assert metrics["regex_searches"] == 1
        assert metrics["other_api_calls"] == 2


# ---------------------------------------------------------------------------
# AC3: BackendRegistry and StorageFactory
# ---------------------------------------------------------------------------


class TestBackendRegistryApiMetricsField:
    """Tests for BackendRegistry.api_metrics field (AC3)."""

    def test_backend_registry_has_api_metrics_field(self):
        """BackendRegistry dataclass must have an 'api_metrics' field."""
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "api_metrics" in fields, (
            "BackendRegistry must have an 'api_metrics' field. "
            f"Current fields: {fields}"
        )

    def test_storage_factory_creates_api_metrics_backend_sqlite_mode(self, tmp_path):
        """StorageFactory._create_sqlite_backends must produce a valid ApiMetricsBackend."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import ApiMetricsBackend

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)

        assert hasattr(registry, "api_metrics"), (
            "BackendRegistry must have .api_metrics attribute"
        )
        assert isinstance(registry.api_metrics, ApiMetricsBackend), (
            f"registry.api_metrics must satisfy ApiMetricsBackend protocol, "
            f"got {type(registry.api_metrics)}"
        )

    def test_storage_factory_api_metrics_backend_is_functional(self, tmp_path):
        """The api_metrics backend from StorageFactory must be able to insert and query."""
        from code_indexer.server.storage.factory import StorageFactory

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)
        api_metrics = registry.api_metrics

        api_metrics.insert_metric("semantic")
        api_metrics.insert_metric("other_api")

        metrics = api_metrics.get_metrics(window_seconds=3600)
        assert metrics["semantic_searches"] == 1
        assert metrics["other_api_calls"] == 1


# ---------------------------------------------------------------------------
# AC4: Node-ID filtering
# ---------------------------------------------------------------------------


class TestApiMetricsSqliteBackendNodeIdFiltering:
    """Tests for node_id filtering in ApiMetricsSqliteBackend (AC4)."""

    @pytest.fixture
    def db_path(self, tmp_path):
        return str(tmp_path / "test_metrics_node.db")

    @pytest.fixture
    def backend(self, db_path):
        from code_indexer.server.storage.sqlite_backends import ApiMetricsSqliteBackend

        b = ApiMetricsSqliteBackend(db_path)
        yield b
        b.close()

    def test_get_metrics_with_node_id_filter(self, backend):
        """get_metrics(node_id=...) must return only metrics for that node."""
        backend.insert_metric("semantic", node_id="node-A")
        backend.insert_metric("semantic", node_id="node-A")
        backend.insert_metric("regex", node_id="node-B")
        backend.insert_metric("other_api", node_id=None)  # standalone / no node

        metrics_node_a = backend.get_metrics(window_seconds=3600, node_id="node-A")
        assert metrics_node_a["semantic_searches"] == 2
        assert metrics_node_a["regex_searches"] == 0

        metrics_node_b = backend.get_metrics(window_seconds=3600, node_id="node-B")
        assert metrics_node_b["regex_searches"] == 1
        assert metrics_node_b["semantic_searches"] == 0

    def test_get_metrics_without_node_id_returns_all(self, backend):
        """get_metrics(node_id=None) must aggregate across all nodes."""
        backend.insert_metric("semantic", node_id="node-A")
        backend.insert_metric("semantic", node_id="node-B")
        backend.insert_metric("regex", node_id=None)

        metrics = backend.get_metrics(window_seconds=3600, node_id=None)
        assert metrics["semantic_searches"] == 2
        assert metrics["regex_searches"] == 1


# ---------------------------------------------------------------------------
# AC5: PostgreSQL backend satisfies protocol (structural check, no live DB)
# ---------------------------------------------------------------------------


class TestApiMetricsPostgresBackendProtocolSatisfaction:
    """Structural test: ApiMetricsPostgresBackend must satisfy ApiMetricsBackend protocol."""

    def test_postgres_backend_has_all_required_methods(self):
        """ApiMetricsPostgresBackend must declare all methods required by the protocol."""
        # Import the class without instantiating (no live DB needed)
        from code_indexer.server.storage.postgres.api_metrics_backend import (
            ApiMetricsPostgresBackend,
        )

        # Check each protocol method is defined on the class
        for method_name in [
            "insert_metric",
            "get_metrics",
            "cleanup_old",
            "reset",
            "close",
        ]:
            assert hasattr(ApiMetricsPostgresBackend, method_name), (
                f"ApiMetricsPostgresBackend must have method '{method_name}'"
            )

    def test_postgres_backend_module_importable(self):
        """The postgres api_metrics_backend module must be importable without a live DB."""
        try:
            from code_indexer.server.storage.postgres import (
                api_metrics_backend,  # noqa: F401
            )
        except ImportError as exc:
            pytest.fail(f"Failed to import api_metrics_backend module: {exc}")
