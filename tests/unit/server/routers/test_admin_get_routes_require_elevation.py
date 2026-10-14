"""Every REST GET under ``/api/admin/`` and ``/api/v1/`` requires elevation,
unless it is on the explicit allowlist below with a written reason.

Admin reads of user data (user lists, emails, group membership, search
history, audit content, credentials) require the caller's own TOTP elevation
window, like ``GET /api/v1/users`` and MCP ``list_users``.  A new GET route
under these prefixes therefore fails this test until it is either gated with
``require_elevation()`` or added here with a reason that says why it carries
no such data.

"Requires elevation" is read from the route's resolved FastAPI dependency
tree on the real app (``create_app`` via ``isolated_app``): some dependency,
at any depth, is a ``require_elevation()`` check built for the ``"full"``
scope.  The check is recognised by its code object (every
``require_elevation()`` call returns a closure over the same code), never by
source text or names.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from code_indexer.server.auth import dependencies
from tests.unit.server._isolated_app import isolated_app

_PREFIXES = ("/api/admin", "/api/v1")

_GIT_HISTORY_READ = (
    "git read of a repository for any user with access to it (repository "
    "permission check). Log, blame and file history DO return commit author "
    "names and emails: kept as a repository-permission read by design (the "
    "repository's own history), not the server's user records"
)

_REPO_READ = (
    "repository read for any user with access to that repository "
    "(repository permission check); not an admin read"
)

# path -> why this GET needs no elevation.  Nothing returning user lists,
# emails, group membership, search history, audit content or credentials may
# be listed here.
ALLOWLIST: Dict[str, str] = {
    "/api/admin/golden-repos": (
        "repository catalog (aliases, branches, URLs with userinfo redacted); "
        "no user data"
    ),
    "/api/admin/golden-repos/{alias}/indexes": "index presence per repository",
    "/api/admin/embedding-stats/query": (
        "per-call provider, model, counts, latency, repository alias and job id; "
        "no user identity or query text"
    ),
    "/api/admin/hnsw-orphan-sweep/stats": "maintenance sweep counters",
    "/api/admin/jobs/stats": "aggregate job counts by status and type",
    "/api/admin/maintenance/status": "maintenance mode flag",
    "/api/admin/maintenance/drain-status": (
        "running-job counts and progress, polled by the auto-updater's restart "
        "drain with a non-elevated token"
    ),
    "/api/admin/maintenance/drain-timeout": "configured drain timeout value",
    "/api/admin/memory-governor": "memory governor snapshot",
    "/api/admin/provider-indexes/health": "provider index health",
    "/api/admin/provider-indexes/providers": "configured provider names",
    "/api/admin/provider-indexes/status": "provider index status of a repository",
    "/api/admin/scip-cleanup-status": "SCIP workspace cleanup job status",
    "/api/admin/siem-delivery/stats": "SIEM delivery counters",
    "/api/admin/siem-delivery/quarantine": (
        "quarantined delivery metadata (event id, action type, reason); "
        "never event bodies"
    ),
    "/api/v1/groups": "group names and descriptions; no members",
    "/api/v1/repo-categories": "repository category catalog, readable by any user",
    "/api/v1/repos/{alias}/git/blame": _GIT_HISTORY_READ,
    "/api/v1/repos/{alias}/git/branches": _GIT_HISTORY_READ,
    "/api/v1/repos/{alias}/git/cat": _GIT_HISTORY_READ,
    "/api/v1/repos/{alias}/git/diff": _GIT_HISTORY_READ,
    "/api/v1/repos/{alias}/git/file-history": _GIT_HISTORY_READ,
    "/api/v1/repos/{alias}/git/log": _GIT_HISTORY_READ,
    "/api/v1/repos/{alias}/git/status": _GIT_HISTORY_READ,
    "/api/v1/repos/{alias}/index-status": _REPO_READ,
    "/api/v1/repos/{alias}/temporal-status": _REPO_READ,
}

# Routes whose gate this test must see; proves the detector reads the tree.
_KNOWN_GATED = ("/api/v1/users", "/api/v1/audit-logs", "/api/admin/users")

_ELEVATION_CODE = dependencies.require_elevation().__code__


@pytest.fixture(scope="module")
def get_routes(tmp_path_factory: pytest.TempPathFactory) -> Iterator[List[APIRoute]]:
    """Every GET route under the prefixes, from the real app."""
    with isolated_app(tmp_path_factory.mktemp("admin-get-routes-app")) as app:
        yield [
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and "GET" in route.methods
            and any(
                route.path == p or route.path.startswith(p + "/") for p in _PREFIXES
            )
        ]


def _is_full_elevation_check(call: Any) -> bool:
    if getattr(call, "__code__", None) is not _ELEVATION_CODE:
        return False
    scope = inspect.getclosurevars(call).nonlocals.get("required_scope")
    return bool(scope == "full")


def _requires_elevation(dependant: Dependant) -> bool:
    """True when some dependency, at any depth, is a full-scope elevation check."""
    pending = list(dependant.dependencies)
    while pending:  # bounded: the dependant tree is finite and acyclic
        node = pending.pop()
        if _is_full_elevation_check(node.call):
            return True
        pending.extend(node.dependencies)
    return False


def test_routes_were_enumerated_and_known_gates_detected(get_routes) -> None:
    by_path = {route.path: route for route in get_routes}
    for path in _KNOWN_GATED:
        assert path in by_path, f"{path} not found among GET routes"
        assert _requires_elevation(by_path[path].dependant), path


def test_every_admin_get_route_requires_elevation_or_is_allowlisted(
    get_routes,
) -> None:
    ungated = sorted(
        route.path
        for route in get_routes
        if route.path not in ALLOWLIST and not _requires_elevation(route.dependant)
    )
    assert ungated == [], (
        "GET routes without require_elevation() and not on the allowlist "
        f"(gate them, or allowlist with a reason): {ungated}"
    )


def test_allowlist_has_no_stale_entries(get_routes) -> None:
    """Each allowlisted path exists and is still ungated, so the list never
    hides a route that has since been removed or gated."""
    by_path = {route.path: route for route in get_routes}
    missing = sorted(p for p in ALLOWLIST if p not in by_path)
    gated = sorted(
        p
        for p in ALLOWLIST
        if p in by_path and _requires_elevation(by_path[p].dependant)
    )
    assert missing == [], f"allowlisted paths with no GET route: {missing}"
    assert gated == [], f"allowlisted paths that are now gated: {gated}"


def test_every_allowlist_entry_has_a_reason() -> None:
    assert all(reason.strip() for reason in ALLOWLIST.values())
