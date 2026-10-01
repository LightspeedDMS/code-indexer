"""NodeMetricsBackend Protocol (Story #492: Cluster-Aware Dashboard).

Moved verbatim from the monolithic protocols.py (Issue #1935 Part 2) --
pure typing-construct relocation, zero behaviour change.
"""

from __future__ import annotations

from ._shared import Any, Dict, List, Protocol, datetime, runtime_checkable


@runtime_checkable
class NodeMetricsBackend(Protocol):
    """Protocol for cluster node metrics storage (Story #492).

    Supports both SQLite (standalone) and PostgreSQL (cluster) backends.
    Each node writes snapshots periodically; the dashboard reads the latest
    snapshot per node to render the cluster health carousel.
    """

    def write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Write a single metrics snapshot for this node.

        Args:
            snapshot: Dict with keys: node_id, node_ip, timestamp, cpu_usage,
                memory_percent, memory_used_bytes, process_rss_mb, index_memory_mb,
                swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                net_rx_kb_s, net_tx_kb_s, volumes_json, server_version.
        """
        ...

    def get_latest_per_node(self) -> List[Dict[str, Any]]:
        """Return the latest snapshot for each distinct node_id.

        Returns:
            List of snapshot dicts, one per distinct node_id, ordered by
            node_id. Each dict has all snapshot fields.
        """
        ...

    def get_all_snapshots(self, since: datetime) -> List[Dict[str, Any]]:
        """Return all snapshots since the given datetime.

        Args:
            since: Datetime cutoff; only snapshots with timestamp >= since are returned.

        Returns:
            List of snapshot dicts ordered by timestamp ascending.
        """
        ...

    def cleanup_older_than(self, cutoff: datetime) -> int:
        """Delete all snapshots with timestamp older than cutoff.

        Args:
            cutoff: Datetime threshold; records with timestamp < cutoff are deleted.

        Returns:
            Number of rows deleted.
        """
        ...

    def close(self) -> None:
        """Close the backend and release any held resources."""
        ...
