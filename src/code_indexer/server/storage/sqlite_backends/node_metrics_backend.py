"""
SQLite backend for cluster node metrics storage (Story #492).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class NodeMetricsSqliteBackend:
    """
    SQLite backend for cluster node metrics storage (Story #492).

    Stores periodic snapshots of per-node system metrics (CPU, memory, disk,
    network) so the dashboard can display cluster health without polling psutil
    directly in the HTTP request path.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file.
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Write a single metrics snapshot for a node.

        Args:
            snapshot: Dict with keys: node_id, node_ip, timestamp, cpu_usage,
                memory_percent, memory_used_bytes, process_rss_mb, index_memory_mb,
                swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                net_rx_kb_s, net_tx_kb_s, volumes_json, server_version.
        """

        def operation(conn: Any) -> None:
            conn.execute(
                """INSERT INTO node_metrics
                   (node_id, node_ip, timestamp, cpu_usage, memory_percent,
                    memory_used_bytes, process_rss_mb, index_memory_mb,
                    swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                    net_rx_kb_s, net_tx_kb_s, volumes_json, server_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot["node_id"],
                    snapshot["node_ip"],
                    snapshot["timestamp"],
                    snapshot["cpu_usage"],
                    snapshot["memory_percent"],
                    snapshot["memory_used_bytes"],
                    snapshot["process_rss_mb"],
                    snapshot["index_memory_mb"],
                    snapshot["swap_used_mb"],
                    snapshot["swap_total_mb"],
                    snapshot["disk_read_kb_s"],
                    snapshot["disk_write_kb_s"],
                    snapshot["net_rx_kb_s"],
                    snapshot["net_tx_kb_s"],
                    snapshot["volumes_json"],
                    snapshot["server_version"],
                ),
            )

        self._conn_manager.execute_atomic(operation)
        logger.debug(
            "Wrote node_metrics snapshot for node: %s", snapshot.get("node_id")
        )

    def get_latest_per_node(self) -> List[Dict[str, Any]]:
        """Return the latest snapshot for each distinct node_id.

        Returns:
            List of snapshot dicts, one per distinct node_id, ordered by
            node_id. Each dict has all snapshot fields.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT nm.node_id, nm.node_ip, nm.timestamp, nm.cpu_usage,
                      nm.memory_percent, nm.memory_used_bytes, nm.process_rss_mb,
                      nm.index_memory_mb, nm.swap_used_mb, nm.swap_total_mb,
                      nm.disk_read_kb_s, nm.disk_write_kb_s, nm.net_rx_kb_s,
                      nm.net_tx_kb_s, nm.volumes_json, nm.server_version
               FROM node_metrics nm
               INNER JOIN (
                   SELECT node_id, MAX(timestamp) AS max_ts
                   FROM node_metrics
                   GROUP BY node_id
               ) latest ON nm.node_id = latest.node_id AND nm.timestamp = latest.max_ts
               ORDER BY nm.node_id"""
        )
        rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_all_snapshots(self, since: "datetime") -> List[Dict[str, Any]]:
        """Return all snapshots since the given datetime.

        Args:
            since: Datetime cutoff; only snapshots with timestamp >= since are returned.

        Returns:
            List of snapshot dicts ordered by timestamp ascending.
        """
        since_str = since.isoformat()
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT node_id, node_ip, timestamp, cpu_usage, memory_percent,
                      memory_used_bytes, process_rss_mb, index_memory_mb,
                      swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                      net_rx_kb_s, net_tx_kb_s, volumes_json, server_version
               FROM node_metrics
               WHERE timestamp >= ?
               ORDER BY timestamp ASC""",
            (since_str,),
        )
        rows = cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    def cleanup_older_than(self, cutoff: "datetime") -> int:
        """Delete all snapshots with timestamp older than cutoff.

        Args:
            cutoff: Datetime threshold; records with timestamp < cutoff are deleted.

        Returns:
            Number of rows deleted.
        """
        cutoff_str = cutoff.isoformat()

        def operation(conn: Any) -> int:
            cursor = conn.execute(
                "DELETE FROM node_metrics WHERE timestamp < ?",
                (cutoff_str,),
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug("Cleaned up %d old node_metrics records", deleted)
        return deleted

    def _row_to_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row tuple to a snapshot dict."""
        return {
            "node_id": row[0],
            "node_ip": row[1],
            "timestamp": row[2],
            "cpu_usage": row[3],
            "memory_percent": row[4],
            "memory_used_bytes": row[5],
            "process_rss_mb": row[6],
            "index_memory_mb": row[7],
            "swap_used_mb": row[8],
            "swap_total_mb": row[9],
            "disk_read_kb_s": row[10],
            "disk_write_kb_s": row[11],
            "net_rx_kb_s": row[12],
            "net_tx_kb_s": row[13],
            "volumes_json": row[14],
            "server_version": row[15],
        }

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()
