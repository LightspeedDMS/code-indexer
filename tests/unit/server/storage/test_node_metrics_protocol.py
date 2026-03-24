"""
Protocol compliance tests for NodeMetricsBackend.

Story #492: Cluster-Aware Dashboard with Node Metrics Carousel

Verifies that backends satisfy the NodeMetricsBackend Protocol.
"""

from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture
def sqlite_backend(tmp_path: Path) -> Generator:
    """Create a NodeMetricsSqliteBackend."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import NodeMetricsSqliteBackend

    db_path = tmp_path / "test.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    yield NodeMetricsSqliteBackend(str(db_path))


class TestNodeMetricsBackendProtocol:
    """Tests that verify NodeMetricsBackend Protocol definition and compliance."""

    def test_protocol_is_importable(self) -> None:
        """NodeMetricsBackend Protocol is importable from protocols module."""
        from code_indexer.server.storage.protocols import NodeMetricsBackend

        assert NodeMetricsBackend is not None

    def test_protocol_is_runtime_checkable(self) -> None:
        """NodeMetricsBackend Protocol supports isinstance() checks."""
        from code_indexer.server.storage.protocols import NodeMetricsBackend
        from code_indexer.server.storage.sqlite_backends import NodeMetricsSqliteBackend

        # Runtime checkable Protocol must work with isinstance()
        backend = NodeMetricsSqliteBackend.__new__(NodeMetricsSqliteBackend)
        assert isinstance(backend, NodeMetricsBackend)

    def test_sqlite_backend_satisfies_protocol(self, sqlite_backend) -> None:
        """NodeMetricsSqliteBackend satisfies NodeMetricsBackend Protocol."""
        from code_indexer.server.storage.protocols import NodeMetricsBackend

        assert isinstance(sqlite_backend, NodeMetricsBackend)

    def test_protocol_has_required_methods(self) -> None:
        """NodeMetricsBackend Protocol defines all required methods."""
        from code_indexer.server.storage.protocols import NodeMetricsBackend

        required_methods = [
            "write_snapshot",
            "get_latest_per_node",
            "get_all_snapshots",
            "cleanup_older_than",
            "close",
        ]
        for method in required_methods:
            assert hasattr(
                NodeMetricsBackend, method
            ), f"Protocol missing method: {method}"

    def test_protocol_close_method_present(self) -> None:
        """NodeMetricsBackend Protocol has a close() method, consistent with other Protocols."""
        from code_indexer.server.storage.protocols import NodeMetricsBackend

        assert hasattr(
            NodeMetricsBackend, "close"
        ), "NodeMetricsBackend Protocol must define close() like all other Protocols"

    def test_sqlite_backend_satisfies_protocol_including_close(
        self, sqlite_backend
    ) -> None:
        """NodeMetricsSqliteBackend satisfies NodeMetricsBackend Protocol including close()."""
        from code_indexer.server.storage.protocols import NodeMetricsBackend

        # Both implementations already have close(); this test guards regressions
        assert isinstance(sqlite_backend, NodeMetricsBackend)
        assert hasattr(sqlite_backend, "close")
        assert callable(sqlite_backend.close)
