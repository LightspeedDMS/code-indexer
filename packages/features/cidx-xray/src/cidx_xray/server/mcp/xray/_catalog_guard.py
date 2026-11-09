"""X-Ray over the internal catalog is admin-only.

Every X-Ray door that runs over a repository (MCP xray_search,
xray_explore, xray_dump_ast, xray_search_batch, analyze_graph and the REST
X-Ray routes) asks ``catalog_xray_refusal`` before any evaluator, file or
job work. X-Ray serves golden repositories only, so the catalog is judged
by the golden alias requested and by the directory its alias pointer
serves. Pattern storage in the catalog (store_xray_pattern) is not search
and is not asked. The policy itself is the core's catalog access projection
(``history_policy``), reached through the server platform.
"""

from __future__ import annotations

import functools
from typing import Any, Dict, Optional

import anyio.to_thread

from cidx_xray._platform import server_platform

from .._common import _lazy_app_attr

XRAY_NOT_AVAILABLE = "xray_not_available"

#: The core's catalog surface X-Ray is judged as (its refusal text).
_CATALOG_SURFACE = "XRAY"


def _app_state() -> Optional[Any]:
    app = _lazy_app_attr("app")
    if app is None:
        return None
    return getattr(app, "state", None)


def xray_access_service() -> Optional[Any]:
    """app.state.access_filtering_service, without constructing the app
    singleton (None when not wired: the policy then fails closed)."""
    state = _app_state()
    if state is None:
        return None
    return getattr(state, "access_filtering_service", None)


def xray_golden_repos_dir() -> Optional[str]:
    """app.state.golden_repos_dir, without constructing the app singleton
    (None when not wired: alias pointers are then not checked)."""
    state = _app_state()
    if state is None:
        return None
    value = getattr(state, "golden_repos_dir", None)
    return str(value) if value else None


def catalog_xray_refusal(
    repo_alias: Any, username: str, access_service: Optional[Any]
) -> Optional[Dict[str, Any]]:
    """The refusal payload when *repo_alias* (one alias or a list) may serve
    the catalog and the caller is restricted, else None; an empty list is
    judged as one value (and refused). Synchronous."""
    aliases = (
        repo_alias if isinstance(repo_alias, list) and repo_alias else [repo_alias]
    )
    golden_repos_dir = xray_golden_repos_dir()
    platform = server_platform()
    for alias in aliases:
        refusal = platform.golden_catalog_surface_refusal(
            _CATALOG_SURFACE, alias, username, access_service, golden_repos_dir
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
    """catalog_xray_refusal() for an async MCP handler, on a worker thread
    (it reads the group store and alias pointers)."""
    return await anyio.to_thread.run_sync(
        functools.partial(
            catalog_xray_refusal, repo_alias, username, xray_access_service()
        )
    )
