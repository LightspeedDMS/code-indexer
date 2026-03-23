"""
Research Assistant Session Router (Story #433).

Routes Research Assistant requests to the correct cluster node.

Conversations are pinned to the node where they were created because the
workspace files live on that node's local filesystem.

In standalone mode (pool=None) all sessions are local.
In cluster mode the router queries the research_sessions table for ownership
and the cluster_nodes table for the owner node's HTTP URL.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# DDL executed lazily when first used in cluster mode.
_ENSURE_NODE_ID_COLUMN_SQL = """
ALTER TABLE research_sessions
    ADD COLUMN IF NOT EXISTS node_id TEXT;
"""


class RASessionRouter:
    """Routes Research Assistant requests to the correct cluster node.

    In standalone mode (pool=None) every method assumes the local node owns
    all sessions.  No database access happens.

    In cluster mode the router:
    - Registers new sessions by writing ``node_id`` into ``research_sessions``.
    - Resolves the owning node's HTTP URL from the ``cluster_nodes`` table
      (``http://{hostname}:{port}``).
    - Answers whether a request should be proxied to a different node.
    """

    def __init__(self, node_id: str, pool: Optional[Any] = None) -> None:
        """
        Initialise the router.

        Args:
            node_id: Unique identifier for this cluster node.
            pool:    PostgreSQL ConnectionPool.  Pass None for standalone mode.
        """
        self._node_id = node_id
        self._pool = pool
        self._column_lock = threading.Lock()
        self._column_ensured = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def node_id(self) -> str:
        """Unique identifier for this cluster node."""
        return self._node_id

    def get_session_owner(self, session_id: str) -> Optional[str]:
        """
        Return the node_id that owns this session.

        In standalone mode always returns ``self._node_id``.
        In cluster mode queries ``research_sessions.node_id``.

        Returns:
            node_id string if found, None if the session does not exist.
        """
        if self._pool is None:
            return self._node_id

        self._ensure_node_id_column()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT node_id FROM research_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cur.fetchone()

        if row is None:
            return None
        # node_id may be NULL in rows created before cluster mode was enabled;
        # treat those as belonging to this node.
        return row[0] if row[0] is not None else self._node_id

    def register_session(self, session_id: str) -> None:
        """
        Register this node as the owner of a new session.

        In standalone mode this is a no-op.
        In cluster mode writes ``node_id`` into ``research_sessions``.

        The session row is assumed to already exist (created by
        ResearchAssistantService).  This method sets the ``node_id``
        column so subsequent routing decisions work correctly.
        """
        if self._pool is None:
            return

        self._ensure_node_id_column()
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE research_sessions
                    SET    node_id = %s
                    WHERE  id = %s
                    """,
                    (self._node_id, session_id),
                )

    def is_local_session(self, session_id: str) -> bool:
        """
        Return True if this session belongs to this node.

        In standalone mode always True.
        """
        owner = self.get_session_owner(session_id)
        if owner is None:
            # Unknown session — treat as local so requests can fail with a
            # meaningful 404 from the RA service rather than a routing error.
            return True
        return owner == self._node_id

    def get_owner_node_url(self, session_id: str) -> Optional[str]:
        """
        Return the HTTP URL of the node that owns this session.

        Format: ``http://{hostname}:{port}``

        Returns None when:
        - standalone mode (pool is None)
        - session not found
        - owner node not found in cluster_nodes

        The caller is responsible for appending the path when proxying.
        """
        if self._pool is None:
            return None

        owner_node_id = self.get_session_owner(session_id)
        if owner_node_id is None:
            return None

        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT hostname, port FROM cluster_nodes WHERE node_id = %s",
                    (owner_node_id,),
                )
                row = cur.fetchone()

        if row is None:
            logger.warning(
                "RASessionRouter: owner node '%s' not found in cluster_nodes",
                owner_node_id,
            )
            return None

        hostname, port = row
        return f"http://{hostname}:{port}"

    def should_proxy(self, session_id: str) -> bool:
        """
        Return True if the request for this session should be proxied.

        A request must be proxied when the owning node is a different cluster
        node from this one.  In standalone mode always returns False.
        """
        if self._pool is None:
            return False
        return not self.is_local_session(session_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_node_id_column(self) -> None:
        """
        Add ``node_id`` column to ``research_sessions`` if absent.

        Idempotent — uses ``ADD COLUMN IF NOT EXISTS`` so repeated calls
        are safe.  Result is cached in ``_column_ensured`` to skip the DDL
        on subsequent calls.
        """
        with self._column_lock:
            if self._column_ensured:
                return
        assert self._pool is not None  # only called from cluster-mode paths
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_ENSURE_NODE_ID_COLUMN_SQL)
        self._column_ensured = True
