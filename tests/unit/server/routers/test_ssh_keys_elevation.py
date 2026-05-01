"""
Structural elevation gate tests for ssh_keys router (Task 2 / P0-B).

Verifies that write endpoints AND the key-list GET have require_elevation() wired.
GET /{name}/public does NOT have elevation (public key material is not sensitive).
Both GET endpoints have admin auth (get_current_admin_user_hybrid).
No HTTP calls — inspects FastAPI route descriptors directly.
"""

import pytest
from fastapi.routing import APIRoute

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"
_ADMIN_AUTH_QUALNAME = "get_current_admin_user_hybrid"

# (path, method, should_have_elevation)
_ROUTE_CASES = [
    # Write endpoints — must be gated
    ("/api/ssh-keys", "POST", True),
    ("/api/ssh-keys/{name}", "DELETE", True),
    ("/api/ssh-keys/{name}/hosts", "POST", True),
    # Key list exposes private paths — must be gated
    ("/api/ssh-keys", "GET", True),
    # Public key is not sensitive — admin auth only, no elevation
    ("/api/ssh-keys/{name}/public", "GET", False),
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


def _route_has_admin_auth(route) -> bool:
    """Return True if get_current_admin_user_hybrid is in the route's dependant deps."""
    for dep in route.dependant.dependencies:
        if getattr(dep.call, "__name__", "") == _ADMIN_AUTH_QUALNAME:
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


@pytest.fixture(scope="module")
def ssh_keys_router():
    from code_indexer.server.routers.ssh_keys import router

    return router


@pytest.mark.parametrize("path,method,expected", _ROUTE_CASES)
def test_ssh_keys_elevation_gate(ssh_keys_router, path, method, expected):
    """Parametrized structural check: elevation present iff expected."""
    route = _find_route(ssh_keys_router, path, method)
    assert route is not None, f"{method} {path} route not found in ssh_keys router"
    has_elev = _route_has_elevation_dep(route)
    if expected:
        assert has_elev, (
            f"{method} {path} must have require_elevation() in its dependencies"
        )
    else:
        assert not has_elev, f"{method} {path} must NOT have require_elevation()"


def test_list_ssh_keys_requires_admin_auth(ssh_keys_router):
    """Gap 3: GET /api/ssh-keys must require admin authentication."""
    route = _find_route(ssh_keys_router, "/api/ssh-keys", "GET")
    assert route is not None, "GET /api/ssh-keys route not found in ssh_keys router"
    assert _route_has_admin_auth(route), (
        "GET /api/ssh-keys must have get_current_admin_user_hybrid dependency"
    )


def test_get_public_key_requires_admin_auth(ssh_keys_router):
    """Gap 3: GET /api/ssh-keys/{name}/public must require admin authentication."""
    route = _find_route(ssh_keys_router, "/api/ssh-keys/{name}/public", "GET")
    assert route is not None, (
        "GET /api/ssh-keys/{name}/public route not found in ssh_keys router"
    )
    assert _route_has_admin_auth(route), (
        "GET /api/ssh-keys/{name}/public must have get_current_admin_user_hybrid dependency"
    )
