"""The node id recorded on search-event rows (search events and query
embedding events).

ONE resolver shared by the search-event writers (Story #2118 promoted it
from search_embed_event_emit.py; mcp/handlers/search/code_search.py and
routers/inline_query.py carried copies of the same lookup). It is NOT the
identity for embedding_call_stats attribution: those rows must join to job
ownership and use ``cluster_node_id.resolve_server_config_node_id``.

Precedence: the runtime config's ``node_id``, else ``socket.gethostname()``,
else ``"unknown"``. Never raises: a node id must never break the write it
annotates.

Note: this is NOT the cluster membership identity
(``server/utils/cluster_node_id.resolve_cluster_node_id``, which reads
``cluster.node_id`` from bootstrap config and defaults to
``<hostname>-cidx``).
"""

from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)


def resolve_runtime_node_id() -> str:
    """Stepwise node_id lookup -- never raises (getattr with defaults)."""
    try:
        from code_indexer.server.services.config_service import get_config_service

        cfg_svc = get_config_service()
        cfg_get = getattr(cfg_svc, "get_config", None)
        cfg_obj = cfg_get() if callable(cfg_get) else None
        node_id = str(getattr(cfg_obj, "node_id", "") or "")
    except Exception as exc:  # noqa: BLE001 -- node_id must never break the write
        logger.debug("resolve_runtime_node_id: config node_id lookup failed: %s", exc)
        node_id = ""

    if not node_id:
        try:
            node_id = socket.gethostname()
        except OSError as exc:
            logger.debug("resolve_runtime_node_id: gethostname() failed: %s", exc)
            node_id = "unknown"
    return node_id
