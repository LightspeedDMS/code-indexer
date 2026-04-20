"""
Dependency-map MCP handlers — Story #855.

Provides depmap_find_consumers_handler and _register() for wiring into
the HANDLER_REGISTRY via _legacy.py.

dep_map_path is resolved fresh on every call via
app.state.dependency_map_service.cidx_meta_read_path — never cached.
"""

import logging
from pathlib import Path
from typing import Any, Dict

from . import _utils
from ._utils import _mcp_response

logger = logging.getLogger(__name__)


def depmap_find_consumers_handler(params: Dict[str, Any], user: Any) -> Dict[str, Any]:
    """
    MCP handler for depmap_find_consumers.

    Reads dep_map_path fresh from app.state on every invocation so that
    path changes from cidx-meta refreshes are always picked up.

    Args:
        params: Tool arguments. Expected key: ``repo_name`` (str).
        user: Authenticated user (unused for path resolution, kept for
              handler signature compatibility).

    Returns:
        MCP-compliant response dict with content array wrapping JSON:
        - success=true:  {"success": true, "consumers": [...], "anomalies": [...]}
        - success=false: {"success": false, "error": "...", "consumers": [], "anomalies": []}
    """
    repo_name = params.get("repo_name", "") if isinstance(params, dict) else ""
    if not isinstance(repo_name, str):
        repo_name = ""

    # Resolve dep_map_path fresh — NEVER cached
    dep_map_path: Path = (
        _utils.app_module.app.state.dependency_map_service.cidx_meta_read_path
    )

    if not dep_map_path.exists():
        logger.warning(
            "depmap_find_consumers: dep_map_path not found: %s", dep_map_path
        )
        return _mcp_response(
            {
                "success": False,
                "error": "dep_map_path not found",
                "consumers": [],
                "anomalies": [],
            }
        )

    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser

    parser = DepMapMCPParser(dep_map_path)
    consumers, anomalies = parser.find_consumers(repo_name)

    return _mcp_response(
        {
            "success": True,
            "consumers": consumers,
            "anomalies": anomalies,
        }
    )


def _register(registry: Dict[str, Any]) -> None:
    """Register depmap handlers in the HANDLER_REGISTRY."""
    registry["depmap_find_consumers"] = depmap_find_consumers_handler
