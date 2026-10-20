"""X-Ray over the internal catalog is admin-only.

Every X-Ray door that runs over a repository (MCP xray_search,
xray_explore, xray_dump_ast, xray_search_batch, analyze_graph and the REST
X-Ray routes) asks ``catalog_xray_refusal`` before any evaluator, file or
job work. X-Ray serves golden repositories only, so the catalog is judged
by the golden alias requested. Pattern storage in the catalog
(store_xray_pattern) is not search and is not asked.
"""

from __future__ import annotations

import functools
from typing import Any, Dict, Optional

import anyio.to_thread

from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
    golden_alias_is_catalog,
    golden_catalog_surface_refusal,
)

from .._utils import _lazy_module_attr_or_none

XRAY_NOT_AVAILABLE = "xray_not_available"


def xray_access_service() -> Optional[Any]:
    """app.state.access_filtering_service, without constructing the app
    singleton (None when not wired: the policy then fails closed)."""
    app = _lazy_module_attr_or_none("app")
    return getattr(getattr(app, "state", None), "access_filtering_service", None)


def catalog_xray_refusal(
    repo_alias: Any, username: str, access_service: Optional[Any]
) -> Optional[Dict[str, Any]]:
    """The refusal payload when *repo_alias* (one alias or a list) names the
    catalog and the caller is restricted, else None. Synchronous."""
    aliases = repo_alias if isinstance(repo_alias, list) else [repo_alias]
    for alias in aliases:
        refusal = golden_catalog_surface_refusal(
            CatalogSurface.XRAY, alias, username, access_service
        )
        if refusal is not None:
            return {
                "error": XRAY_NOT_AVAILABLE,
                "message": refusal,
                "repository_alias": alias,
            }
    return None


async def catalog_xray_refusal_async(
    repo_alias: Any, username: str
) -> Optional[Dict[str, Any]]:
    """catalog_xray_refusal() for an async MCP handler. Naming the catalog is
    a pure check; only then is the visibility lookup (group store reads) run
    on a worker thread."""
    aliases = repo_alias if isinstance(repo_alias, list) else [repo_alias]
    if not any(golden_alias_is_catalog(alias) for alias in aliases):
        return None
    return await anyio.to_thread.run_sync(
        functools.partial(
            catalog_xray_refusal, repo_alias, username, xray_access_service()
        )
    )
