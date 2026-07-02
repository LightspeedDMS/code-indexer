"""
Structural elevation gate tests for groups router (Task 6 / P1-B).

Verifies that all write endpoints have require_elevation() wired,
and that the 2 GET endpoints do NOT have elevation.
Also verifies users_router (PUT /api/v1/users/{user_id}/group) has elevation.
Also verifies users_router GET and audit_router GET have elevation (Gaps 1 & 2).
No HTTP calls — inspects FastAPI route descriptors directly.
"""

import pytest
from fastapi.routing import APIRoute

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"

# (path, method, should_have_elevation)
_ROUTE_CASES = [
    # Write endpoints — must be gated
    ("/api/v1/groups", "POST", True),
    ("/api/v1/groups/{group_id}", "PUT", True),
    ("/api/v1/groups/{group_id}", "DELETE", True),  # Gap 1: delete_group
    ("/api/v1/groups/{group_id}/members", "POST", True),
    ("/api/v1/groups/{group_id}/repos", "POST", True),
    ("/api/v1/groups/{group_id}/repos/{repo_name}", "DELETE", True),
    ("/api/v1/groups/{group_id}/repos", "DELETE", True),
    # Read endpoints — must NOT be gated
    ("/api/v1/groups", "GET", False),
    ("/api/v1/groups/{group_id}", "GET", False),
]


def _route_has_elevation_dep(route) -> bool:
    """Return True if require_elevation() is wired to the given route."""
    for dep in getattr(route, "dependencies", []):
        dep_callable = getattr(dep, "dependency", None)
        if dep_callable is None:
            continue
        if getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME:
            return True
    return False


def _find_route(router, path: str, method: str):
    """Find a specific route by path and HTTP method."""
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == path and method in (route.methods or []):
            return route
    return None


def _assert_route_elevation(router, path: str, method: str, expected: bool) -> None:
    """Assert that a route has (or does not have) elevation, with a clear message."""
    route = _find_route(router, path, method)
    assert route is not None, f"{method} {path} route not found in router"
    has_elev = _route_has_elevation_dep(route)
    if expected:
        assert has_elev, (
            f"{method} {path} must have require_elevation() in its dependencies"
        )
    else:
        assert not has_elev, f"{method} {path} must NOT have require_elevation()"


@pytest.fixture(scope="module")
def groups_router():
    from code_indexer.server.routers.groups import router

    return router


@pytest.fixture(scope="module")
def users_router():
    from code_indexer.server.routers.groups import users_router as _users_router

    return _users_router


@pytest.fixture(scope="module")
def audit_router():
    from code_indexer.server.routers.groups import audit_router as _audit_router

    return _audit_router


@pytest.mark.parametrize("path,method,expected", _ROUTE_CASES)
def test_groups_elevation_gate(groups_router, path, method, expected):
    """Parametrized structural check: elevation present iff expected."""
    _assert_route_elevation(groups_router, path, method, expected)


def test_move_user_to_group_has_elevation(users_router):
    """Gap 2: PUT /api/v1/users/{user_id}/group must require elevation."""
    _assert_route_elevation(users_router, "/api/v1/users/{user_id}/group", "PUT", True)


def test_list_users_requires_elevation(users_router):
    """Gap 1: GET /api/v1/users must require elevation (lists sensitive user data)."""
    _assert_route_elevation(users_router, "/api/v1/users", "GET", True)


def test_get_audit_logs_requires_elevation(audit_router):
    """Gap 2: GET /api/v1/audit-logs must require elevation (sensitive audit data)."""
    _assert_route_elevation(audit_router, "/api/v1/audit-logs", "GET", True)
