"""
PostgreSQL-based rolling upgrade coordination service.

Story #431: Rolling Upgrade with PostgreSQL Upgrade Registry

Ensures only one node upgrades at a time during rolling cluster upgrades.
Uses the upgrade_registry table to coordinate between nodes.

Table schema (created lazily on first use):
    CREATE TABLE IF NOT EXISTS upgrade_registry (
        id          SERIAL PRIMARY KEY,
        node_id     TEXT        NOT NULL,
        version_from TEXT       NOT NULL,
        version_to   TEXT       NOT NULL,
        status       TEXT        NOT NULL DEFAULT 'upgrading',
        started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        error_message TEXT
    )

Only one row with status='upgrading' may exist at any time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

_CREATE_UPGRADE_REGISTRY = """
CREATE TABLE IF NOT EXISTS upgrade_registry (
    id            SERIAL       PRIMARY KEY,
    node_id       TEXT         NOT NULL,
    version_from  TEXT         NOT NULL,
    version_to    TEXT         NOT NULL,
    status        TEXT         NOT NULL DEFAULT 'upgrading',
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    error_message TEXT
)
"""

_CREATE_UNIQUE_UPGRADING_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS upgrade_registry_one_upgrading
    ON upgrade_registry (status)
    WHERE status = 'upgrading'
"""


class UpgradeRegistryService:
    """
    PostgreSQL-based rolling upgrade coordination.

    Guarantees at most one node has status='upgrading' in the upgrade_registry
    table at any moment by exploiting a partial unique index on
    (status) WHERE status = 'upgrading'.

    Thread-safety and cross-process safety are both provided by PostgreSQL
    constraint enforcement — no application-level locking is required.
    """

    def __init__(self, pool: "ConnectionPool", node_id: str) -> None:
        """
        Initialise the service.

        Args:
            pool: A ConnectionPool instance (from connection_pool.py).
            node_id: Unique identifier for this node (e.g. hostname or UUID).
        """
        self._pool = pool
        self._node_id = node_id
        self._table_ensured = False

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _ensure_table(self) -> None:
        """
        Create the upgrade_registry table and unique index if they do not exist.

        Idempotent — safe to call on every operation.  Uses a module-level
        flag to avoid redundant DDL round-trips after the first call.
        """
        if self._table_ensured:
            return
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_CREATE_UPGRADE_REGISTRY)
                cur.execute(_CREATE_UNIQUE_UPGRADING_INDEX)
        self._table_ensured = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_upgrade(self) -> bool:
        """
        Check whether it is safe for this node to begin upgrading.

        Returns True when no other node currently has status='upgrading'.
        Returns False when another node is actively upgrading.
        """
        self._ensure_table()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT node_id FROM upgrade_registry WHERE status = 'upgrading' LIMIT 1"
                )
                row = cur.fetchone()
        return row is None

    def begin_upgrade(self, version_from: str, version_to: str) -> bool:
        """
        Register this node as upgrading.

        Inserts a row with status='upgrading' only when no other row with
        that status already exists (enforced by partial unique index).

        Args:
            version_from: The version being upgraded from.
            version_to:   The version being upgraded to.

        Returns:
            True  — row inserted; this node owns the upgrade lock.
            False — another node is already upgrading; try again later.
        """
        self._ensure_table()
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO upgrade_registry (node_id, version_from, version_to, status)
                        VALUES (%s, %s, %s, 'upgrading')
                        """,
                        (self._node_id, version_from, version_to),
                    )
            logger.info(
                "Node %s began upgrade %s -> %s",
                self._node_id,
                version_from,
                version_to,
            )
            return True
        except Exception as exc:
            # Unique constraint violation means another node is upgrading.
            # Any other exception is unexpected and should propagate.
            err_str = str(exc).lower()
            if "unique" in err_str or "duplicate" in err_str:
                logger.info(
                    "Node %s could not acquire upgrade lock (another node is upgrading): %s",
                    self._node_id,
                    exc,
                )
                return False
            raise

    def complete_upgrade(self) -> None:
        """
        Mark this node's in-progress upgrade as completed.

        Updates the row owned by this node with status='upgrading' to
        status='completed' and records the completion timestamp.

        Raises:
            RuntimeError: if no upgrading row is found for this node.
        """
        self._ensure_table()
        completed_at = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE upgrade_registry
                    SET status = 'completed', completed_at = %s
                    WHERE node_id = %s AND status = 'upgrading'
                    """,
                    (completed_at, self._node_id),
                )
                if cur.rowcount == 0:
                    raise RuntimeError(
                        f"No upgrading row found for node '{self._node_id}'; "
                        "cannot complete upgrade."
                    )
        logger.info("Node %s completed upgrade", self._node_id)

    def fail_upgrade(self, error_message: str) -> None:
        """
        Mark this node's in-progress upgrade as failed.

        Updates the row owned by this node with status='upgrading' to
        status='failed', records the error message and completion timestamp.

        Args:
            error_message: Human-readable description of the failure.

        Raises:
            RuntimeError: if no upgrading row is found for this node.
        """
        self._ensure_table()
        completed_at = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE upgrade_registry
                    SET status = 'failed', completed_at = %s, error_message = %s
                    WHERE node_id = %s AND status = 'upgrading'
                    """,
                    (completed_at, error_message, self._node_id),
                )
                if cur.rowcount == 0:
                    raise RuntimeError(
                        f"No upgrading row found for node '{self._node_id}'; "
                        "cannot record upgrade failure."
                    )
        logger.warning("Node %s upgrade failed: %s", self._node_id, error_message)

    def get_upgrade_history(self, limit: int = 20) -> List[dict]:
        """
        Return recent upgrade records ordered by start time (newest first).

        Args:
            limit: Maximum number of records to return (default 20).

        Returns:
            List of dicts with keys: id, node_id, version_from, version_to,
            status, started_at, completed_at, error_message.
        """
        self._ensure_table()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, node_id, version_from, version_to,
                           status, started_at, completed_at, error_message
                    FROM upgrade_registry
                    ORDER BY started_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_current_upgrading_node(self) -> Optional[str]:
        """
        Return the node_id of the node currently upgrading, or None.

        Returns:
            node_id string if a node has status='upgrading', else None.
        """
        self._ensure_table()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT node_id FROM upgrade_registry WHERE status = 'upgrading' LIMIT 1"
                )
                row = cur.fetchone()
        return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row) -> dict:
    """Convert a psycopg row (sequence) to an upgrade record dict."""
    return {
        "id": row[0],
        "node_id": row[1],
        "version_from": row[2],
        "version_to": row[3],
        "status": row[4],
        "started_at": row[5],
        "completed_at": row[6],
        "error_message": row[7],
    }
