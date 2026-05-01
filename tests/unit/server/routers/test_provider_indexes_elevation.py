"""
Structural elevation gate tests for provider_indexes router.

Verifies that all write (mutating) endpoints have require_elevation() wired,
and that the GET (read) endpoints do NOT have elevation.
No HTTP calls — inspects FastAPI route descriptors directly.
"""

import pytest
from fastapi.routing import APIRoute

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"

# (path, method, should_have_elevation)
_ROUTE_CASES = [
    # Write endpoints — must be gated
    ("/api/admin/provider-indexes/add", "POST", True),
    ("/api/admin/provider-indexes/recreate", "POST", True),
    ("/api/admin/provider-indexes/remove", "POST", True),
    ("/api/admin/provider-indexes/bulk-add", "POST", True),
    # Read endpoints — must NOT be gated
    ("/api/admin/provider-indexes/providers", "GET", False),
    ("/api/admin/provider-indexes/status", "GET", False),
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
def provider_indexes_router():
    from code_indexer.server.routers.provider_indexes import router

    return router


@pytest.mark.parametrize("path,method,expected", _ROUTE_CASES)
def test_provider_indexes_elevation_gate(
    provider_indexes_router, path, method, expected
):
    """Parametrized structural check: elevation present iff expected."""
    route = _find_route(provider_indexes_router, path, method)
    assert route is not None, (
        f"{method} {path} route not found in provider_indexes router"
    )
    has_elev = _route_has_elevation_dep(route)
    if expected:
        assert has_elev, (
            f"{method} {path} must have require_elevation() in its dependencies"
        )
    else:
        assert not has_elev, f"{method} {path} must NOT have require_elevation()"
