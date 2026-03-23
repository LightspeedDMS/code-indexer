"""
Cluster Health Provider.

Provides cluster-specific health information when the server is running in
cluster mode. All dependencies are optional — the provider degrades gracefully
when a dependency is not available (None).

Used by the health endpoint to extend the standard health response with
cluster topology data: node identity, leader/follower role, NFS mount health,
and PostgreSQL connectivity.
"""

import time
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ClusterHealthProvider:
    """Provides cluster-specific health information."""

    def __init__(
        self,
        node_id: str,
        leader_election_service: Optional[Any] = None,
        nfs_validator: Optional[Any] = None,
        pg_pool: Optional[Any] = None,
    ) -> None:
        """
        Initialise the provider.

        Args:
            node_id: Unique identifier for this cluster node.
            leader_election_service: Optional service with ``is_leader() -> bool``
                and ``is_active() -> bool`` methods.
            nfs_validator: Optional validator with ``get_mount_path() -> str`` and
                ``is_mounted() -> bool`` methods.
            pg_pool: Optional PostgreSQL connection pool with ``getconn()`` /
                ``putconn()`` methods and a connection supporting
                ``cursor()`` / ``execute()`` / ``fetchone()``.
        """
        self._node_id = node_id
        self._leader_election = leader_election_service
        self._nfs_validator = nfs_validator
        self._pg_pool = pg_pool
        self._start_time = time.time()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_cluster_health(self) -> Dict[str, Any]:
        """
        Return a cluster health status dictionary.

        Returns:
            {
                "cluster_mode": True,
                "node": {
                    "node_id": "cidx-node-01",
                    "role": "leader" | "follower",
                    "storage_mode": "postgres",
                    "uptime_seconds": 3621
                },
                "checks": {
                    "postgresql": {"status": "healthy", "latency_ms": 2},
                    "nfs_mount": {"status": "healthy", "path": "/mnt/cidx-shared"},
                    "leader_election": {"status": "active", "is_leader": True}
                }
            }
        """
        uptime = time.time() - self._start_time
        role = self._get_role()

        checks: Dict[str, Any] = {
            "postgresql": self._check_postgresql(),
            "nfs_mount": self._check_nfs_mount(),
            "leader_election": self._check_leader_election(),
        }

        return {
            "cluster_mode": True,
            "node": {
                "node_id": self._node_id,
                "role": role,
                "storage_mode": "postgres",
                "uptime_seconds": int(uptime),
            },
            "checks": checks,
        }

    def is_healthy(self) -> bool:
        """
        Quick cluster health check.

        Returns False if any critical check (postgresql, nfs_mount) fails.
        The leader_election check is informational and does not affect the
        overall healthy flag.
        """
        pg_check = self._check_postgresql()
        nfs_check = self._check_nfs_mount()

        return (
            pg_check.get("status") == "healthy" and nfs_check.get("status") == "healthy"
        )

    def get_standalone_health(self) -> Dict[str, Any]:
        """
        Return health information for standalone (non-cluster) mode.

        Backward compatible: callers that do not run in cluster mode receive
        a minimal dict with ``cluster_mode: False``.
        """
        return {
            "cluster_mode": False,
            "storage_mode": "sqlite",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_role(self) -> str:
        """Determine node role via leader_election_service."""
        if self._leader_election is None:
            return "unknown"
        try:
            return "leader" if self._leader_election.is_leader else "follower"
        except Exception as exc:
            logger.warning("leader_election_service.is_leader() raised: %s", exc)
            return "unknown"

    def _check_postgresql(self) -> Dict[str, Any]:
        """Check PostgreSQL connectivity and measure round-trip latency."""
        if self._pg_pool is None:
            return {"status": "unavailable", "latency_ms": None}

        start = time.time()
        try:
            with self._pg_pool.connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()

            latency_ms = int((time.time() - start) * 1000)
            return {"status": "healthy", "latency_ms": latency_ms}

        except Exception as exc:
            logger.warning("PostgreSQL health check failed: %s", exc)
            latency_ms = int((time.time() - start) * 1000)
            return {
                "status": "unhealthy",
                "latency_ms": latency_ms,
                "error": str(exc),
            }

    def _check_nfs_mount(self) -> Dict[str, Any]:
        """Check NFS mount availability."""
        if self._nfs_validator is None:
            return {"status": "unavailable", "path": None}

        try:
            path = self._nfs_validator.get_mount_path()
            mounted = self._nfs_validator.is_mounted()
            if mounted:
                return {"status": "healthy", "path": path}
            else:
                return {"status": "unhealthy", "path": path}
        except Exception as exc:
            logger.warning("NFS mount health check failed: %s", exc)
            return {"status": "unhealthy", "path": None, "error": str(exc)}

    def _check_leader_election(self) -> Dict[str, Any]:
        """Check leader election service status."""
        if self._leader_election is None:
            return {"status": "unavailable", "is_leader": None}

        try:
            is_leader = self._leader_election.is_leader
            is_active = True
            try:
                is_active = self._leader_election.is_active()
            except AttributeError:
                pass  # Optional method — not all implementations expose it

            status = "active" if is_active else "inactive"
            return {"status": status, "is_leader": is_leader}
        except Exception as exc:
            logger.warning("Leader election health check failed: %s", exc)
            return {"status": "error", "is_leader": None, "error": str(exc)}
