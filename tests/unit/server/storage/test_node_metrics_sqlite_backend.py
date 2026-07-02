"""
Unit tests for NodeMetricsSqliteBackend - SQLite storage for cluster node metrics.

Story #492: Cluster-Aware Dashboard with Node Metrics Carousel

Tests written FIRST following TDD methodology.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional

import pytest


@pytest.fixture
def backend(tmp_path: Path) -> Generator:
    """Create a NodeMetricsSqliteBackend with initialized database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import NodeMetricsSqliteBackend

    db_path = tmp_path / "test.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    yield NodeMetricsSqliteBackend(str(db_path))


def _make_snapshot(
    node_id: str = "node-1",
    node_ip: str = "192.168.1.1",
    timestamp: Optional[datetime] = None,
    cpu_usage: float = 25.0,
    memory_percent: float = 60.0,
    memory_used_bytes: int = 4 * 1024 * 1024 * 1024,
    process_rss_mb: float = 120.0,
    index_memory_mb: float = 50.0,
    swap_used_mb: float = 200.0,
    swap_total_mb: float = 8192.0,
    disk_read_kb_s: float = 10.0,
    disk_write_kb_s: float = 5.0,
    net_rx_kb_s: float = 100.0,
    net_tx_kb_s: float = 50.0,
    volumes_json: Optional[str] = None,
    server_version: str = "8.0.0",
) -> dict:
    """Helper to create a snapshot dict with sensible defaults."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    if volumes_json is None:
        volumes_json = json.dumps([{"mount_point": "/", "used_percent": 45.0}])
    return {
        "node_id": node_id,
        "node_ip": node_ip,
        "timestamp": timestamp.isoformat(),
        "cpu_usage": cpu_usage,
        "memory_percent": memory_percent,
        "memory_used_bytes": memory_used_bytes,
        "process_rss_mb": process_rss_mb,
        "index_memory_mb": index_memory_mb,
        "swap_used_mb": swap_used_mb,
        "swap_total_mb": swap_total_mb,
        "disk_read_kb_s": disk_read_kb_s,
        "disk_write_kb_s": disk_write_kb_s,
        "net_rx_kb_s": net_rx_kb_s,
        "net_tx_kb_s": net_tx_kb_s,
        "volumes_json": volumes_json,
        "server_version": server_version,
    }


@pytest.mark.slow
class TestNodeMetricsSqliteBackendWriteSnapshot:
    """Tests for write_snapshot method."""

    def test_write_snapshot_inserts_row(self, backend, tmp_path: Path) -> None:
        """write_snapshot() inserts a row into node_metrics table."""
        snapshot = _make_snapshot()
        backend.write_snapshot(snapshot)

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute("SELECT node_id, node_ip FROM node_metrics").fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "node-1"
        assert row[1] == "192.168.1.1"

    def test_write_snapshot_stores_all_fields(self, backend, tmp_path: Path) -> None:
        """write_snapshot() stores all metric fields correctly."""
        volumes = [{"mount_point": "/data", "used_percent": 72.5}]
        snapshot = _make_snapshot(
            node_id="node-42",
            node_ip="10.0.0.42",
            cpu_usage=88.5,
            memory_percent=75.3,
            memory_used_bytes=12 * 1024 * 1024 * 1024,
            process_rss_mb=256.8,
            index_memory_mb=1024.0,
            swap_used_mb=512.0,
            swap_total_mb=4096.0,
            disk_read_kb_s=2048.5,
            disk_write_kb_s=1024.3,
            net_rx_kb_s=8192.0,
            net_tx_kb_s=4096.0,
            volumes_json=json.dumps(volumes),
            server_version="8.1.0",
        )
        backend.write_snapshot(snapshot)

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute(
            """SELECT node_id, node_ip, cpu_usage, memory_percent, memory_used_bytes,
               process_rss_mb, index_memory_mb, swap_used_mb, swap_total_mb,
               disk_read_kb_s, disk_write_kb_s, net_rx_kb_s, net_tx_kb_s,
               volumes_json, server_version
               FROM node_metrics WHERE node_id='node-42'"""
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "node-42"
        assert row[1] == "10.0.0.42"
        assert abs(row[2] - 88.5) < 0.01
        assert abs(row[3] - 75.3) < 0.01
        assert row[4] == 12 * 1024 * 1024 * 1024
        assert abs(row[5] - 256.8) < 0.01
        assert abs(row[6] - 1024.0) < 0.01
        assert abs(row[7] - 512.0) < 0.01
        assert abs(row[8] - 4096.0) < 0.01
        assert abs(row[9] - 2048.5) < 0.01
        assert abs(row[10] - 1024.3) < 0.01
        assert abs(row[11] - 8192.0) < 0.01
        assert abs(row[12] - 4096.0) < 0.01
        stored_volumes = json.loads(row[13])
        assert stored_volumes[0]["mount_point"] == "/data"
        assert row[14] == "8.1.0"

    def test_write_snapshot_multiple_nodes(self, backend, tmp_path: Path) -> None:
        """write_snapshot() can store snapshots from multiple nodes."""
        backend.write_snapshot(_make_snapshot(node_id="node-1", node_ip="192.168.1.1"))
        backend.write_snapshot(_make_snapshot(node_id="node-2", node_ip="192.168.1.2"))

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        count = conn.execute("SELECT COUNT(*) FROM node_metrics").fetchone()[0]
        conn.close()

        assert count == 2

    def test_write_snapshot_same_node_multiple_times(
        self, backend, tmp_path: Path
    ) -> None:
        """write_snapshot() stores multiple snapshots for same node (time series)."""
        t1 = datetime.now(timezone.utc) - timedelta(seconds=10)
        t2 = datetime.now(timezone.utc)

        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=t1))
        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=t2))

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM node_metrics WHERE node_id='node-1'"
        ).fetchone()[0]
        conn.close()

        assert count == 2


class TestNodeMetricsSqliteBackendGetLatestPerNode:
    """Tests for get_latest_per_node method."""

    def test_get_latest_per_node_empty_returns_empty_list(self, backend) -> None:
        """get_latest_per_node() returns empty list when no snapshots exist."""
        result = backend.get_latest_per_node()
        assert result == []

    def test_get_latest_per_node_returns_one_per_node(self, backend) -> None:
        """get_latest_per_node() returns exactly one snapshot per distinct node_id."""
        t_old = datetime.now(timezone.utc) - timedelta(seconds=30)
        t_new = datetime.now(timezone.utc)

        # Two snapshots for node-1 (different times)
        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=t_old))
        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=t_new))
        # One snapshot for node-2
        backend.write_snapshot(_make_snapshot(node_id="node-2", timestamp=t_new))

        result = backend.get_latest_per_node()

        assert len(result) == 2
        node_ids = {r["node_id"] for r in result}
        assert node_ids == {"node-1", "node-2"}

    def test_get_latest_per_node_returns_most_recent_snapshot(self, backend) -> None:
        """get_latest_per_node() returns the LATEST snapshot for each node."""
        t_old = datetime.now(timezone.utc) - timedelta(seconds=30)
        t_new = datetime.now(timezone.utc)

        backend.write_snapshot(
            _make_snapshot(node_id="node-1", timestamp=t_old, cpu_usage=10.0)
        )
        backend.write_snapshot(
            _make_snapshot(node_id="node-1", timestamp=t_new, cpu_usage=90.0)
        )

        result = backend.get_latest_per_node()

        assert len(result) == 1
        assert abs(result[0]["cpu_usage"] - 90.0) < 0.01

    def test_get_latest_per_node_includes_all_fields(self, backend) -> None:
        """get_latest_per_node() includes all metric fields in returned dicts."""
        backend.write_snapshot(_make_snapshot())
        result = backend.get_latest_per_node()

        assert len(result) == 1
        row = result[0]

        # Verify all required fields are present
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


class TestNodeMetricsSqliteBackendCleanupOlderThan:
    """Tests for cleanup_older_than method."""

    def test_cleanup_older_than_deletes_old_records(self, backend, tmp_path) -> None:
        """cleanup_older_than() deletes records older than cutoff datetime."""
        old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        new_ts = datetime.now(timezone.utc)

        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=old_ts))
        backend.write_snapshot(_make_snapshot(node_id="node-2", timestamp=new_ts))

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        deleted = backend.cleanup_older_than(cutoff)

        assert deleted == 1

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        count = conn.execute("SELECT COUNT(*) FROM node_metrics").fetchone()[0]
        conn.close()
        assert count == 1

    def test_cleanup_older_than_returns_deleted_count(self, backend) -> None:
        """cleanup_older_than() returns the number of rows deleted."""
        old_ts = datetime.now(timezone.utc) - timedelta(hours=3)

        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=old_ts))
        backend.write_snapshot(_make_snapshot(node_id="node-2", timestamp=old_ts))

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        deleted = backend.cleanup_older_than(cutoff)

        assert deleted == 2

    def test_cleanup_older_than_preserves_recent_records(
        self, backend, tmp_path
    ) -> None:
        """cleanup_older_than() does not delete records newer than cutoff."""
        recent_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=recent_ts))

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        deleted = backend.cleanup_older_than(cutoff)

        assert deleted == 0
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        count = conn.execute("SELECT COUNT(*) FROM node_metrics").fetchone()[0]
        conn.close()
        assert count == 1

    def test_cleanup_older_than_empty_table_returns_zero(self, backend) -> None:
        """cleanup_older_than() returns 0 when table is empty."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        deleted = backend.cleanup_older_than(cutoff)
        assert deleted == 0


class TestNodeMetricsSqliteBackendGetAllSnapshots:
    """Tests for get_all_snapshots method."""

    def test_get_all_snapshots_returns_snapshots_since(self, backend) -> None:
        """get_all_snapshots() returns snapshots since specified datetime."""
        old_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        recent_ts = datetime.now(timezone.utc) - timedelta(minutes=30)

        backend.write_snapshot(_make_snapshot(node_id="node-1", timestamp=old_ts))
        backend.write_snapshot(_make_snapshot(node_id="node-2", timestamp=recent_ts))

        since = datetime.now(timezone.utc) - timedelta(hours=1)
        result = backend.get_all_snapshots(since=since)

        assert len(result) == 1
        assert result[0]["node_id"] == "node-2"

    def test_get_all_snapshots_returns_all_when_old_cutoff(self, backend) -> None:
        """get_all_snapshots() with old cutoff returns all snapshots."""
        for i in range(3):
            ts = datetime.now(timezone.utc) - timedelta(minutes=i * 5)
            backend.write_snapshot(_make_snapshot(node_id=f"node-{i}", timestamp=ts))

        since = datetime.now(timezone.utc) - timedelta(hours=24)
        result = backend.get_all_snapshots(since=since)

        assert len(result) == 3
