"""
PostgreSQL backend for cluster node metrics storage (Story #492).

Drop-in replacement for NodeMetricsSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the NodeMetricsBackend Protocol
(protocols.py).

Table used:
    node_metrics  -- created by migration 003_node_metrics.sql
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class NodeMetricsPostgresBackend:
    """
    PostgreSQL backend for cluster node metrics storage.

    Satisfies the NodeMetricsBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool

    def write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Write a single metrics snapshot for a node."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO node_metrics
                    (node_id, node_ip, timestamp, cpu_usage, memory_percent,
                     memory_used_bytes, process_rss_mb, index_memory_mb,
                     swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                     net_rx_kb_s, net_tx_kb_s, volumes_json, server_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
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
            conn.commit()

        logger.debug(
            "Wrote node_metrics snapshot for node: %s", snapshot.get("node_id")
        )

    def get_latest_per_node(self) -> List[Dict[str, Any]]:
        """Return the latest snapshot for each distinct node_id."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT ON (nm.node_id)
                    nm.node_id, nm.node_ip, nm.timestamp, nm.cpu_usage,
                    nm.memory_percent, nm.memory_used_bytes, nm.process_rss_mb,
                    nm.index_memory_mb, nm.swap_used_mb, nm.swap_total_mb,
                    nm.disk_read_kb_s, nm.disk_write_kb_s, nm.net_rx_kb_s,
                    nm.net_tx_kb_s, nm.volumes_json, nm.server_version
                FROM node_metrics nm
                ORDER BY nm.node_id, nm.timestamp DESC
                """
            ).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def get_all_snapshots(self, since: datetime) -> List[Dict[str, Any]]:
        """Return all snapshots since the given datetime."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT node_id, node_ip, timestamp, cpu_usage, memory_percent,
                       memory_used_bytes, process_rss_mb, index_memory_mb,
                       swap_used_mb, swap_total_mb, disk_read_kb_s, disk_write_kb_s,
                       net_rx_kb_s, net_tx_kb_s, volumes_json, server_version
                FROM node_metrics
                WHERE timestamp >= %s
                ORDER BY timestamp ASC
                """,
                (since,),
            ).fetchall()

        return [self._row_to_dict(row) for row in rows]

    def cleanup_older_than(self, cutoff: datetime) -> int:
        """Delete all snapshots with timestamp older than cutoff."""
        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM node_metrics WHERE timestamp < %s",
                (cutoff,),
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()

        if deleted:
            logger.debug("Cleaned up %d old node_metrics records", deleted)
        return deleted

    def _row_to_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row tuple to a snapshot dict."""
        # volumes_json may come back as a dict/list (JSONB) from PostgreSQL
        volumes_json = row[14]
        if not isinstance(volumes_json, str):
            volumes_json = json.dumps(volumes_json)

        # timestamp may be a datetime object from PostgreSQL
        timestamp = row[2]
        if isinstance(timestamp, datetime):
            ts_str = timestamp.isoformat()
        else:
            ts_str = str(timestamp)

        return {
            "node_id": row[0],
            "node_ip": row[1],
            "timestamp": ts_str,
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
            "volumes_json": volumes_json,
            "server_version": row[15],
        }

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
