"""
Unit tests for NodeMetricsWriterService - Background service writing node metrics.

Story #492: Cluster-Aware Dashboard with Node Metrics Carousel

Tests written FIRST following TDD methodology.
Minimal mocking: only mock the threading.Event.wait() to speed up tests.
Real backend (in-memory SQLite) is used.
"""

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def db_backend(tmp_path: Path):
    """Create a real NodeMetricsSqliteBackend for integration-style testing."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import NodeMetricsSqliteBackend

    db_path = tmp_path / "test.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    return NodeMetricsSqliteBackend(str(db_path))


class TestNodeMetricsWriterServiceInit:
    """Tests for NodeMetricsWriterService initialization."""

    def test_service_uses_hostname_as_node_id_when_no_config(self, db_backend) -> None:
        """When no node_id configured, service uses socket.gethostname()."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(backend=db_backend)
        expected = socket.gethostname()
        assert service.node_id == expected

    def test_service_uses_configured_node_id(self, db_backend) -> None:
        """When node_id is configured, service uses that value."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(backend=db_backend, node_id="my-custom-node")
        assert service.node_id == "my-custom-node"

    def test_service_detects_node_ip(self, db_backend) -> None:
        """Service detects local IP address (non-loopback preferred)."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(backend=db_backend)
        # Should return a valid IP string (not empty)
        assert service.node_ip
        assert isinstance(service.node_ip, str)
        # Should not be empty string
        assert len(service.node_ip) > 0


class TestNodeMetricsWriterServiceWriteOnce:
    """Tests for single write cycle behavior."""

    def test_write_once_inserts_snapshot(self, db_backend) -> None:
        """write_once() inserts a snapshot row into the backend."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(
            backend=db_backend, node_id="test-node", write_interval=60
        )
        service.write_once()

        result = db_backend.get_latest_per_node()
        assert len(result) == 1
        assert result[0]["node_id"] == "test-node"

    def test_write_once_stores_required_fields(self, db_backend) -> None:
        """write_once() stores all required metric fields."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(
            backend=db_backend, node_id="test-node", write_interval=60
        )
        service.write_once()

        result = db_backend.get_latest_per_node()
        assert len(result) == 1
        row = result[0]

        required_fields = [
            "node_id",
            "node_ip",
            "timestamp",
            "cpu_usage",
            "memory_percent",
            "memory_used_bytes",
            "process_rss_mb",
            "index_memory_mb",
            "swap_used_mb",
            "swap_total_mb",
            "disk_read_kb_s",
            "disk_write_kb_s",
            "net_rx_kb_s",
            "net_tx_kb_s",
            "volumes_json",
            "server_version",
        ]
        for field in required_fields:
            assert field in row, f"Missing field: {field}"

    def test_write_once_stores_numeric_metrics(self, db_backend) -> None:
        """write_once() stores numeric metrics as floats/ints."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(
            backend=db_backend, node_id="test-node", write_interval=60
        )
        service.write_once()

        result = db_backend.get_latest_per_node()
        row = result[0]

        # Numeric fields should be numeric types
        assert isinstance(row["cpu_usage"], (int, float))
        assert isinstance(row["memory_percent"], (int, float))
        assert isinstance(row["memory_used_bytes"], (int, float))
        assert isinstance(row["process_rss_mb"], (int, float))
        assert isinstance(row["index_memory_mb"], (int, float))
        # Values should be in valid ranges
        assert 0 <= row["cpu_usage"] <= 100
        assert 0 <= row["memory_percent"] <= 100

    def test_write_once_stores_valid_timestamp(self, db_backend) -> None:
        """write_once() stores an ISO timestamp in the snapshot."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        before = datetime.now(timezone.utc)
        service = NodeMetricsWriterService(
            backend=db_backend, node_id="test-node", write_interval=60
        )
        service.write_once()
        after = datetime.now(timezone.utc)

        result = db_backend.get_latest_per_node()
        ts_str = result[0]["timestamp"]
        ts = datetime.fromisoformat(ts_str)
        # Normalize to UTC for comparison
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        assert before <= ts <= after

    def test_write_once_stores_volumes_json(self, db_backend) -> None:
        """write_once() stores volumes as valid JSON string."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(
            backend=db_backend, node_id="test-node", write_interval=60
        )
        service.write_once()

        result = db_backend.get_latest_per_node()
        volumes_json = result[0]["volumes_json"]
        # Should be valid JSON
        volumes = json.loads(volumes_json)
        assert isinstance(volumes, list)

    def test_write_once_triggers_cleanup(self, db_backend) -> None:
        """write_once() triggers cleanup of records older than retention period."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        # Pre-populate with old snapshot
        old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        db_backend.write_snapshot(
            {
                "node_id": "old-node",
                "node_ip": "1.2.3.4",
                "timestamp": old_ts.isoformat(),
                "cpu_usage": 5.0,
                "memory_percent": 10.0,
                "memory_used_bytes": 1000000,
                "process_rss_mb": 50.0,
                "index_memory_mb": 10.0,
                "swap_used_mb": 0.0,
                "swap_total_mb": 0.0,
                "disk_read_kb_s": 0.0,
                "disk_write_kb_s": 0.0,
                "net_rx_kb_s": 0.0,
                "net_tx_kb_s": 0.0,
                "volumes_json": "[]",
                "server_version": "8.0.0",
            }
        )

        service = NodeMetricsWriterService(
            backend=db_backend,
            node_id="test-node",
            write_interval=60,
            retention_seconds=3600,  # 1 hour - old record should be cleaned
        )
        service.write_once()

        # Old node's old snapshot should be gone; only current node's snapshot remains
        all_nodes = db_backend.get_latest_per_node()
        node_ids = {n["node_id"] for n in all_nodes}
        assert "old-node" not in node_ids


class TestIOMetricsRateCalculation:
    """Tests for I/O rate calculation logic (delta counters / elapsed time)."""

    def test_collect_io_metrics_returns_zero_rates_on_first_call(self) -> None:
        """On first call (no prior snapshot), all I/O rates are 0.0."""
        from code_indexer.server.services.node_metrics_writer_service import (
            _collect_io_metrics_with_state,
        )

        state: dict = {}
        result = _collect_io_metrics_with_state(state)

        assert result["disk_read_kb_s"] == 0.0
        assert result["disk_write_kb_s"] == 0.0
        assert result["net_rx_kb_s"] == 0.0
        assert result["net_tx_kb_s"] == 0.0

    def test_collect_io_metrics_computes_delta_rate(self) -> None:
        """On subsequent calls, I/O rates are delta bytes / elapsed seconds / 1024."""
        from code_indexer.server.services.node_metrics_writer_service import (
            _collect_io_metrics_with_state,
        )
        from unittest.mock import MagicMock

        # Simulate first snapshot: 1 MB read, 0.5 MB write, 2 MB recv, 1 MB sent
        fake_io_1 = MagicMock()
        fake_io_1.read_bytes = 1 * 1024 * 1024
        fake_io_1.write_bytes = 512 * 1024
        fake_net_1 = MagicMock()
        fake_net_1.bytes_recv = 2 * 1024 * 1024
        fake_net_1.bytes_sent = 1 * 1024 * 1024

        t0 = 1000.0

        state: dict = {}
        with (
            patch("psutil.disk_io_counters", return_value=fake_io_1),
            patch("psutil.net_io_counters", return_value=fake_net_1),
            patch("time.monotonic", return_value=t0),
        ):
            _collect_io_metrics_with_state(state)  # First call - all zeros

        # Simulate second snapshot: 3 MB read (+2 MB), 2 MB write (+1.5 MB), 6 MB recv (+4 MB), 3 MB sent (+2 MB)
        # Time elapsed: 10 seconds
        fake_io_2 = MagicMock()
        fake_io_2.read_bytes = 3 * 1024 * 1024  # delta = 2 MB
        fake_io_2.write_bytes = 2 * 1024 * 1024  # delta = 1.5 MB
        fake_net_2 = MagicMock()
        fake_net_2.bytes_recv = 6 * 1024 * 1024  # delta = 4 MB
        fake_net_2.bytes_sent = 3 * 1024 * 1024  # delta = 2 MB

        t1 = t0 + 10.0  # 10 seconds elapsed

        with (
            patch("psutil.disk_io_counters", return_value=fake_io_2),
            patch("psutil.net_io_counters", return_value=fake_net_2),
            patch("time.monotonic", return_value=t1),
        ):
            result = _collect_io_metrics_with_state(state)

        # delta bytes / elapsed / 1024 = KB/s
        # disk_read: 2 MB delta / 10 s / 1024 = 204.8 KB/s
        assert abs(result["disk_read_kb_s"] - 204.8) < 0.01
        # disk_write: 1.5 MB delta / 10 s / 1024 = 153.6 KB/s
        assert abs(result["disk_write_kb_s"] - 153.6) < 0.01
        # net_rx: 4 MB delta / 10 s / 1024 = 409.6 KB/s
        assert abs(result["net_rx_kb_s"] - 409.6) < 0.01
        # net_tx: 2 MB delta / 10 s / 1024 = 204.8 KB/s
        assert abs(result["net_tx_kb_s"] - 204.8) < 0.01

    def test_collect_io_metrics_handles_counter_reset(self) -> None:
        """If counter goes backward (OS reboot/reset), rates are non-negative (clamped to 0)."""
        from code_indexer.server.services.node_metrics_writer_service import (
            _collect_io_metrics_with_state,
        )
        from unittest.mock import MagicMock

        fake_io_1 = MagicMock()
        fake_io_1.read_bytes = 5 * 1024 * 1024
        fake_io_1.write_bytes = 5 * 1024 * 1024
        fake_net_1 = MagicMock()
        fake_net_1.bytes_recv = 5 * 1024 * 1024
        fake_net_1.bytes_sent = 5 * 1024 * 1024

        state: dict = {}
        with (
            patch("psutil.disk_io_counters", return_value=fake_io_1),
            patch("psutil.net_io_counters", return_value=fake_net_1),
            patch("time.monotonic", return_value=1000.0),
        ):
            _collect_io_metrics_with_state(state)

        # Simulate counter reset: values are lower than previous
        fake_io_2 = MagicMock()
        fake_io_2.read_bytes = 1 * 1024 * 1024  # less than before
        fake_io_2.write_bytes = 1 * 1024 * 1024
        fake_net_2 = MagicMock()
        fake_net_2.bytes_recv = 1 * 1024 * 1024
        fake_net_2.bytes_sent = 1 * 1024 * 1024

        with (
            patch("psutil.disk_io_counters", return_value=fake_io_2),
            patch("psutil.net_io_counters", return_value=fake_net_2),
            patch("time.monotonic", return_value=1010.0),
        ):
            result = _collect_io_metrics_with_state(state)

        # All rates should be 0.0 (negative delta clamped to 0)
        assert result["disk_read_kb_s"] == 0.0
        assert result["disk_write_kb_s"] == 0.0
        assert result["net_rx_kb_s"] == 0.0
        assert result["net_tx_kb_s"] == 0.0

    def test_collect_io_metrics_handles_zero_elapsed_time(self) -> None:
        """If elapsed time is zero (very fast back-to-back calls), no division by zero."""
        from code_indexer.server.services.node_metrics_writer_service import (
            _collect_io_metrics_with_state,
        )
        from unittest.mock import MagicMock

        fake_io = MagicMock()
        fake_io.read_bytes = 1024 * 1024
        fake_io.write_bytes = 1024 * 1024
        fake_net = MagicMock()
        fake_net.bytes_recv = 1024 * 1024
        fake_net.bytes_sent = 1024 * 1024

        state: dict = {}
        with (
            patch("psutil.disk_io_counters", return_value=fake_io),
            patch("psutil.net_io_counters", return_value=fake_net),
            patch("time.monotonic", return_value=1000.0),
        ):
            _collect_io_metrics_with_state(state)

        # Same timestamp (elapsed = 0) should not raise
        fake_io_2 = MagicMock()
        fake_io_2.read_bytes = 2 * 1024 * 1024
        fake_io_2.write_bytes = 2 * 1024 * 1024
        fake_net_2 = MagicMock()
        fake_net_2.bytes_recv = 2 * 1024 * 1024
        fake_net_2.bytes_sent = 2 * 1024 * 1024

        with (
            patch("psutil.disk_io_counters", return_value=fake_io_2),
            patch("psutil.net_io_counters", return_value=fake_net_2),
            patch("time.monotonic", return_value=1000.0),
        ):  # same time
            result = _collect_io_metrics_with_state(state)

        # Should return 0.0 instead of raising ZeroDivisionError
        assert result["disk_read_kb_s"] == 0.0
        assert result["disk_write_kb_s"] == 0.0

    def test_write_once_io_rates_are_not_cumulative(self, db_backend) -> None:
        """write_once() stores I/O rates (KB/s), not cumulative totals.

        On first call, rates are 0.0.
        On second call, rates are the delta since first call divided by elapsed seconds.
        Rates must be much smaller than the total cumulative bytes/1024 would be.
        """
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )
        from unittest.mock import MagicMock

        # Use large byte counters so that cumulative values would be very large
        # but delta-based rates will be small and bounded
        LARGE_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB cumulative
        DELTA_BYTES = 100 * 1024  # 100 KB delta per 10s interval

        fake_io_1 = MagicMock()
        fake_io_1.read_bytes = LARGE_BYTES
        fake_io_1.write_bytes = LARGE_BYTES
        fake_net_1 = MagicMock()
        fake_net_1.bytes_recv = LARGE_BYTES
        fake_net_1.bytes_sent = LARGE_BYTES

        fake_io_2 = MagicMock()
        fake_io_2.read_bytes = LARGE_BYTES + DELTA_BYTES
        fake_io_2.write_bytes = LARGE_BYTES + DELTA_BYTES
        fake_net_2 = MagicMock()
        fake_net_2.bytes_recv = LARGE_BYTES + DELTA_BYTES
        fake_net_2.bytes_sent = LARGE_BYTES + DELTA_BYTES

        service = NodeMetricsWriterService(
            backend=db_backend, node_id="rate-test", write_interval=60
        )

        # First write: all I/O rates should be 0.0 (no prior state)
        with (
            patch("psutil.disk_io_counters", return_value=fake_io_1),
            patch("psutil.net_io_counters", return_value=fake_net_1),
            patch("time.monotonic", return_value=1000.0),
        ):
            service.write_once()

        rows = db_backend.get_latest_per_node()
        assert len(rows) == 1
        first_row = rows[0]
        # On first call, rates are 0.0
        assert first_row["disk_read_kb_s"] == 0.0
        assert first_row["net_rx_kb_s"] == 0.0

        # Second write: rates should reflect delta (100 KB / 10 s = 10 KB/s)
        with (
            patch("psutil.disk_io_counters", return_value=fake_io_2),
            patch("psutil.net_io_counters", return_value=fake_net_2),
            patch("time.monotonic", return_value=1010.0),
        ):
            service.write_once()

        rows = db_backend.get_latest_per_node()
        second_row = rows[0]

        # Rate = 100 KB / 10 s = 10.0 KB/s
        expected_rate = 10.0
        assert abs(second_row["disk_read_kb_s"] - expected_rate) < 0.1, (
            f"Expected ~{expected_rate} KB/s but got {second_row['disk_read_kb_s']} "
            f"(cumulative would be {LARGE_BYTES / 1024:.0f} KB)"
        )
        # Rate must be << LARGE_BYTES/1024 to prove it's not cumulative
        assert second_row["disk_read_kb_s"] < LARGE_BYTES / 1024


class TestNodeMetricsWriterServiceStartStop:
    """Tests for background thread start/stop lifecycle."""

    def test_start_launches_background_thread(self, db_backend) -> None:
        """start() launches a background daemon thread."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(
            backend=db_backend, node_id="test-node", write_interval=100
        )

        # Intercept the stop_event.wait to avoid real sleeping
        write_count = [0]
        original_write = service.write_once

        def counting_write():
            write_count[0] += 1
            original_write()
            # Stop after first write to avoid infinite loop in test
            service.stop()

        service.write_once = counting_write
        service.start()

        # Wait for thread to complete
        service._thread.join(timeout=5.0)
        assert write_count[0] >= 1

    def test_stop_signals_thread_to_exit(self, db_backend) -> None:
        """stop() signals the background thread to exit cleanly."""
        from code_indexer.server.services.node_metrics_writer_service import (
            NodeMetricsWriterService,
        )

        service = NodeMetricsWriterService(
            backend=db_backend, node_id="test-node", write_interval=60
        )
        service.start()
        service.stop()

        # Thread should terminate after stop()
        if service._thread is not None:
            service._thread.join(timeout=5.0)
            assert not service._thread.is_alive()
