"""Lock-in: every door that exposes branch or commit metadata, or moves a
clone's HEAD, is classified.

History of the internal catalog is admin-only. Each MCP tool and REST route
with branch or history semantics is listed here as GUARDED (it asks the
catalog history policy; refusal proven by behaviour in the named test
module) or EXEMPT (with the reason it serves no catalog history to a
restricted caller). Candidates are discovered from the live MCP registry
and from every router, so a new tool or route with such semantics fails
this test until it is classified.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from types import SimpleNamespace
from typing import Dict, Set, Tuple

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from tests.shared.cidx_meta_visibility_fixture import RESTRICTED_USER

from . import test_meta_history_surfaces_denied as _history
from .test_meta_history_surfaces_denied import CATALOG_CLONE, _call, _user

# The shared real-repository fixtures, re-exported for pytest.
module_tmp = _history.module_tmp
fx = _history.fx
server = _history.server

_SEMANTICS = re.compile(
    r"branch|commit|history|blame|revision|checkout|\bHEAD\b|git_", re.IGNORECASE
)
_ROUTE_SEMANTICS = re.compile(
    r"branch|commit|history|blame|revision|checkout|/git/|temporal", re.IGNORECASE
)

# Guarded MCP tools outside the git_* family, with the argument naming the
# repository. Every git_* tool is guarded (test_meta_history_surfaces_denied).
MCP_GUARDED: Dict[str, str] = {
    "get_branches": "repository_alias",
    "switch_branch": "user_alias",
}
MCP_EXEMPT: Dict[str, str] = {
    "activate_repository": "activating the catalog is refused by the activation rule",
    "add_golden_repo": "admin only (manage_golden_repos)",
    "change_golden_repo_branch": "admin only (manage_golden_repos)",
    "configure_git_credential": "stores a forge credential; serves no repository",
    "delete_git_credential": "removes a forge credential; serves no repository",
    "list_git_credentials": "lists the caller's own forge credentials",
    "create_pull_request": "acts on the forge with the caller's own token",
    "scip_pr_history": "admin audit records (manage_users), not git history",
    "scip_cleanup_history": "admin audit records (manage_users), not git history",
}

_GIT_ROUTER_PREFIX = "/api/v1/repos/{alias}/git/"
Route = Tuple[str, str]
# Guarded REST routes besides the git router (every route under
# _GIT_ROUTER_PREFIX is guarded: test_meta_history_rest_denied).
REST_GUARDED: Set[Route] = {
    ("GET", "/api/repos/{user_alias}/branches"),
    ("GET", "/api/repos/golden/{alias}/branches"),
    ("GET", "/api/repositories/{repo_id}/branches"),
    ("PUT", "/api/repos/{user_alias}/branch"),
    ("GET", "/api/activated-repos/{user_alias}/branches"),
    ("POST", "/api/activated-repos/{user_alias}/branch"),
}
REST_EXEMPT: Dict[Route, str] = {
    ("GET", "/golden-repos/{alias}/branches"): "Web UI admin session only",
    ("POST", "/golden-repos/{alias}/change-branch"): "Web UI admin session only",
    ("POST", "/golden-repos/{alias}/temporal-options"): "Web UI admin session only",
    ("POST", "/api/discovery/branches"): "remote discovery, serves no repository",
    ("GET", "/api/v1/repos/{alias}/temporal-status"): "index status, no history",
}


def _mcp_candidates() -> Set[str]:
    return {
        name
        for name, tool in TOOL_REGISTRY.items()
        if _SEMANTICS.search(name)
        or _SEMANTICS.search(str(tool.get("description", ""))[:400])
    }


def _all_routes() -> Set[Route]:
    import code_indexer.server.routers as routers_pkg
    import code_indexer.server.routes as routes_pkg
    from code_indexer.server.routers.inline_repos import register_repo_routes
    from code_indexer.server.routers.inline_repos_v2 import register_repos_v2_routes

    modules = [
        importlib.import_module(f"{pkg.__name__}.{info.name}")
        for pkg in (routers_pkg, routes_pkg)
        for info in pkgutil.iter_modules(pkg.__path__)
    ]
    modules.append(importlib.import_module("code_indexer.server.web.routes"))
    found: Set[Route] = set()
    routes = []
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                routes.extend(value.routes)
    app = FastAPI()
    register_repo_routes(
        app,
        activated_repo_manager=None,
        golden_repo_manager=None,
        repository_listing_manager=None,
        background_job_manager=None,
    )
    register_repos_v2_routes(
        app,
        activated_repo_manager=None,
        repository_listing_manager=None,
        background_job_manager=None,
    )
    routes.extend(app.routes)
    for route in routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                found.add((method, route.path))
    return found


def test_every_mcp_branch_or_history_tool_is_classified() -> None:
    git_tools = {name for name in TOOL_REGISTRY if name.startswith("git_")}
    classified = git_tools | set(MCP_GUARDED) | set(MCP_EXEMPT)
    unclassified = _mcp_candidates() - classified
    assert not unclassified, f"classify these tools: {sorted(unclassified)}"
    missing = (set(MCP_GUARDED) | set(MCP_EXEMPT)) - set(TOOL_REGISTRY)
    assert not missing, f"classified tools no longer registered: {sorted(missing)}"


def test_every_rest_branch_or_history_route_is_classified() -> None:
    routes = _all_routes()
    candidates = {r for r in routes if _ROUTE_SEMANTICS.search(r[1])}
    classified = REST_GUARDED | set(REST_EXEMPT)
    unclassified = {
        r for r in candidates - classified if not r[1].startswith(_GIT_ROUTER_PREFIX)
    }
    assert not unclassified, f"classify these routes: {sorted(unclassified)}"
    missing = classified - routes
    assert not missing, f"classified routes no longer exist: {sorted(missing)}"


@pytest.mark.parametrize("tool", sorted(MCP_GUARDED))
def test_guarded_mcp_tools_refuse_the_catalog_for_restricted_caller(
    server: SimpleNamespace, tool: str
) -> None:
    args = {MCP_GUARDED[tool]: CATALOG_CLONE, "branch_name": "main"}
    data = _call(tool, args, _user(RESTRICTED_USER, UserRole.NORMAL_USER))
    assert data.get("error") == CatalogSurface.GIT_HISTORY.refusal, data
