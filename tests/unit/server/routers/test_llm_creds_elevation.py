"""
Structural elevation gate tests for llm_creds router (Task 3 / P0-C).

Verifies that the save-config endpoint has require_elevation() wired,
and that test-connection and lease-status endpoints do NOT have elevation.
No HTTP calls — inspects FastAPI route descriptors directly.
"""

import pytest
from fastapi.routing import APIRoute

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"

# (path, method, should_have_elevation)
_ROUTE_CASES = [
    # Write endpoint — must be gated
    ("/api/llm-creds/save-config", "POST", True),
    # Read/test endpoints — must NOT be gated
    ("/api/llm-creds/test-connection", "POST", False),
    ("/api/llm-creds/lease-status", "GET", False),
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
def llm_creds_router():
    from code_indexer.server.routers.llm_creds import router

    return router


@pytest.mark.parametrize("path,method,expected", _ROUTE_CASES)
def test_llm_creds_elevation_gate(llm_creds_router, path, method, expected):
    """Parametrized structural check: elevation present iff expected."""
    route = _find_route(llm_creds_router, path, method)
    assert route is not None, f"{method} {path} route not found in llm_creds router"
    has_elev = _route_has_elevation_dep(route)
    if expected:
        assert has_elev, (
            f"{method} {path} must have require_elevation() in its dependencies"
        )
    else:
        assert not has_elev, f"{method} {path} must NOT have require_elevation()"
