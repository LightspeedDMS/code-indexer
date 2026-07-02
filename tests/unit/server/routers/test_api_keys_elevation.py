"""
Structural elevation gate tests for api_keys router (Task 1 / P0-A).

Verifies that the 6 write endpoints have require_elevation() wired,
and that test/test-configured endpoints do NOT have elevation.
No HTTP calls — inspects FastAPI route descriptors directly.
"""

import pytest
from fastapi.routing import APIRoute

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"

# (path, method, should_have_elevation)
# Paths include the router prefix /api/api-keys/
_ROUTE_CASES = [
    # Write endpoints — must be gated
    ("/api/api-keys/anthropic", "POST", True),
    ("/api/api-keys/voyageai", "POST", True),
    ("/api/api-keys/cohere", "POST", True),
    ("/api/api-keys/cohere", "DELETE", True),
    ("/api/api-keys/anthropic", "DELETE", True),
    ("/api/api-keys/voyageai", "DELETE", True),
    # Test/status endpoints — must NOT be gated
    ("/api/api-keys/anthropic/test", "POST", False),
    ("/api/api-keys/voyageai/test", "POST", False),
    ("/api/api-keys/cohere/test", "POST", False),
    ("/api/api-keys/anthropic/test-configured", "POST", False),
    ("/api/api-keys/voyageai/test-configured", "POST", False),
    ("/api/api-keys/cohere/test-configured", "POST", False),
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


@pytest.fixture(scope="module")
def api_keys_router():
    from code_indexer.server.routers.api_keys import router

    return router


@pytest.mark.parametrize("path,method,expected", _ROUTE_CASES)
def test_api_keys_elevation_gate(api_keys_router, path, method, expected):
    """Parametrized structural check: elevation present iff expected."""
    route = _find_route(api_keys_router, path, method)
    assert route is not None, f"{method} {path} route not found in api_keys router"
    has_elev = _route_has_elevation_dep(route)
    if expected:
        assert has_elev, (
            f"{method} {path} must have require_elevation() in its dependencies"
        )
    else:
        assert not has_elev, f"{method} {path} must NOT have require_elevation()"
