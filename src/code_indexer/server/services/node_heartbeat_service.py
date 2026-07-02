"""
Node Heartbeat Service (Story #422).

Maintains a heartbeat for this cluster node in the ``cluster_nodes`` table.
Other services (e.g. JobReconciliationService) use this table to determine
which nodes are currently alive when reclaiming abandoned jobs.

The service:
- Registers this node (upsert) on startup.
- Updates the heartbeat timestamp every ``heartbeat_interval`` seconds
  (default 10 s) from a daemon background thread.
- Marks the node as 'offline' on graceful shutdown.
- Exposes ``get_active_nodes()`` which returns node IDs whose heartbeat
  is within the last ``active_threshold_seconds`` (default 30 s).

Table DDL expected by this service::

    CREATE TABLE IF NOT EXISTS cluster_nodes (
        node_id        TEXT PRIMARY KEY,
        status         TEXT NOT NULL DEFAULT 'online',
        last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

This module is cluster-only and must only be loaded when
storage_mode="postgres".  No SQLite dependency.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# Extra seconds added to heartbeat_interval when joining the thread on stop().
_THREAD_JOIN_GRACE_SECONDS = 5

# DDL to create the cluster_nodes table if absent (idempotent).
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cluster_nodes (
    node_id        TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'online',
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class NodeHeartbeatService:
    """
    Maintain a heartbeat record for this cluster node in PostgreSQL.

    Lifecycle::

        service = NodeHeartbeatService(pool, "node-abc")
        service.start()           # registers node, starts beat thread
        ...
        service.stop()            # marks node offline, stops thread
    """

    def __init__(
        self,
        pool: Any,
        node_id: str,
        heartbeat_interval: int = 10,
        active_threshold_seconds: int = 30,
    ) -> None:
        """
        Initialise the service.

        Args:
            pool:                     A ConnectionPool instance.
            node_id:                  Unique identifier for this node.
            heartbeat_interval:       Seconds between heartbeat updates
                                      (default 10).
            active_threshold_seconds: How recent a heartbeat must be for a
                                      node to be considered active
                                      (default 30).
        """
        self._pool = pool
        self._node_id = node_id
        self._heartbeat_interval = heartbeat_interval
        self._active_threshold_seconds = active_threshold_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def node_id(self) -> str:
        """The node identifier this service manages."""
        return self._node_id

    def start(self) -> None:
        """
        Register this node and start the heartbeat background thread.

        Idempotent — if the thread is already running this is a no-op.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("NodeHeartbeatService [%s]: already running", self._node_id)
            return

        self._ensure_table()
        self._upsert_node(status="online")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name=f"NodeHeartbeat-{self._node_id}",
        )
        self._thread.start()
        logger.info(
            "NodeHeartbeatService [%s]: started (interval=%ds)",
            self._node_id,
            self._heartbeat_interval,
        )

    def stop(self) -> None:
        """
        Stop the heartbeat thread and mark this node as 'offline'.
        """
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(
                timeout=self._heartbeat_interval + _THREAD_JOIN_GRACE_SECONDS
            )
        self._thread = None

        try:
            self._upsert_node(status="offline")
        except Exception:
            logger.exception(
                "NodeHeartbeatService [%s]: error marking node offline",
                self._node_id,
            )

        logger.info("NodeHeartbeatService [%s]: stopped", self._node_id)

    def get_active_nodes(self) -> List[str]:
        """
        Return node IDs whose heartbeat is within the active threshold.

        Returns:
            List of ``node_id`` strings for all nodes with
            ``last_heartbeat >= NOW() - active_threshold_seconds``
            and ``status = 'online'``.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT node_id
                    FROM   cluster_nodes
                    WHERE  status = 'online'
                      AND  last_heartbeat >= NOW() - %s * INTERVAL '1 second'
                    """,
                    (self._active_threshold_seconds,),
                )
                rows = cur.fetchall()
        return [row[0] for row in rows]

    def set_leader_election(self, leader_election: Any) -> None:
        """Set leader election service reference for role updates."""
        self._leader_election = leader_election

    def update_heartbeat(self) -> None:
        """
        Perform a single heartbeat update (exposed for testing).
        """
        role = "worker"
        if hasattr(self, "_leader_election") and self._leader_election is not None:
            role = "scheduler" if self._leader_election.is_leader else "worker"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE cluster_nodes
                    SET    last_heartbeat = NOW(),
                           status        = 'online',
                           role          = %s
                    WHERE  node_id = %s
                    """,
                    (role, self._node_id),
                )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """No-op: table creation handled by MigrationRunner (Story #519).

        Bug #547: The original DDL here was a minimal 3-column schema that
        mismatched the richer migration schema. Since migrations auto-run
        on startup before this service starts, the table always exists.
        """
        pass

    def _upsert_node(self, status: str) -> None:
        """Insert or update this node's row with the given status."""
        import os

        hostname = os.uname().nodename
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cluster_nodes (node_id, hostname, status, last_heartbeat)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (node_id) DO UPDATE
                        SET status         = EXCLUDED.status,
                            hostname       = EXCLUDED.hostname,
                            last_heartbeat = NOW()
                    """,
                    (self._node_id, hostname, status),
                )
            conn.commit()

    def _heartbeat_loop(self) -> None:
        """Background loop: update heartbeat every heartbeat_interval seconds."""
        while not self._stop_event.is_set():
            try:
                self.update_heartbeat()
                logger.debug(
                    "NodeHeartbeatService [%s]: heartbeat updated", self._node_id
                )
            except Exception:
                logger.exception(
                    "NodeHeartbeatService [%s]: error updating heartbeat",
                    self._node_id,
                )
            self._stop_event.wait(self._heartbeat_interval)
