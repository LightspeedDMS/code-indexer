"""
Bug #956: Admin Web UI mutations bypass TOTP elevation enforcement.

CI gate ensuring every POST/PUT/DELETE/PATCH route registered under the
web_router (mounted at /admin) either:

  (a) has require_elevation() in its FastAPI dependencies list, OR
  (b) is on the allowlist of routes that must remain ungated to avoid
      login/setup deadlocks.

Exempt routes in _EXEMPT_ROUTES (all non-mutation or bootstrap-critical):
  - GET /logout  -- session termination; gating creates a deadlock
  - GET /elevate -- the elevation form itself; gating it is circular
  - POST /query, POST /partials/query-results -- read-only search form
    submissions that use POST for HTML form conventions but mutate no state

Test suite:
  test_ungated_routes_table               -- structural CI gate
  test_user_mutation_routes_require_elevation -- key user-CRUD routes wired
  test_config_totp_elevation_route_requires_elevation -- config handler gated
  test_exempt_routes_accessible_without_elevation -- logout/elevate not gated
"""

import tempfile
from pathlib import Path
from typing import Optional, cast

import httpx
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Closure __qualname__ produced by require_elevation() — used to detect whether
# a route dependency was wired from that specific factory.
_ELEVATION_QUALNAME = "require_elevation.<locals>._check"

# Routes (method, path_template relative to web_router) that MUST remain
# ungated. Justifications are in the module docstring above.
_EXEMPT_ROUTES: frozenset = frozenset(
    [
        # Session termination — gating creates a deadlock (admin cannot log out
        # if they have no active elevation window).
        ("GET", "/logout"),
        # Elevation form — gating it is circular (you need elevation to open
        # the page where you enter your TOTP code to get elevation).
        ("GET", "/elevate"),
        # Read-only search: POST is used for HTML form submission, but these
        # routes perform no server-state mutation.
        ("POST", "/query"),
        ("POST", "/partials/query-results"),
    ]
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _route_has_elevation_dep(route) -> bool:
    """Return True if route.dependencies contains a require_elevation closure."""
    for dep in getattr(route, "dependencies", []):
        dep_callable = getattr(dep, "dependency", None)
        if dep_callable is None:
            continue
        if getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME:
            return True
    return False


def _find_route(path: str, method: Optional[str] = None):
    """Return the first APIRoute in web_router matching path (and method)."""
    from fastapi.routing import APIRoute

    from code_indexer.server.web.routes import web_router

    for route in web_router.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path != path:
            continue
        if method is not None and method.upper() not in (route.methods or []):
            continue
        return route
    return None


def _is_exempt(method: str, path: str) -> bool:
    return (method.upper(), path) in _EXEMPT_ROUTES


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def app_with_db(tmpdir_path):
    from unittest.mock import patch

    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(str(tmpdir_path / "test.db")).initialize_database()
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
        reset_config_service()
        app = create_app()
        yield app
        reset_config_service()


@pytest.fixture
def client(app_with_db):
    with TestClient(app_with_db, follow_redirects=False) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminElevationGating:
    """CI gate tests for bug #956 admin web UI elevation enforcement."""

    def test_ungated_routes_table(self):
        """CI gate: every non-exempt mutation route in web_router must have
        require_elevation() wired as a FastAPI dependency.

        This test introspects the route registry so it automatically fails
        when a new mutation route ships without the dependency.
        """
        from fastapi.routing import APIRoute

        from code_indexer.server.web.routes import web_router

        ungated: list = []
        for route in web_router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or []:
                if method.upper() not in ("POST", "PUT", "DELETE", "PATCH"):
                    continue
                if _is_exempt(method, route.path):
                    continue
                if not _route_has_elevation_dep(route):
                    ungated.append(f"{method.upper()} {route.path}")

        assert ungated == [], (
            "The following web_router mutation routes lack require_elevation() — "
            "add dependencies=[Depends(dependencies.require_elevation())] "
            "to each decorator:\n"
            + "\n".join(f"  {r}" for r in sorted(ungated))
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/users/create",
            "/users/{username}/role",
            "/users/{username}/password",
            "/users/{username}/email",
            "/users/{username}/delete",
        ],
    )
    def test_user_mutation_routes_require_elevation(self, path: str):
        """Each user-management POST route must have require_elevation wired."""
        route = _find_route(path, method="POST")
        assert route is not None, f"POST {path!r} not registered in web_router"
        assert _route_has_elevation_dep(route), (
            f"POST {path} is missing require_elevation() dependency."
        )

    def test_config_totp_elevation_route_requires_elevation(self):
        """POST /config/{section} — the generic handler that covers the
        totp_elevation kill-switch — must be elevation-gated.

        Without gating, an unelevated admin could disable the kill switch
        itself and bypass all future elevation checks.
        """
        route = _find_route("/config/{section}", method="POST")
        assert route is not None, "POST /config/{section} not found in web_router"
        assert _route_has_elevation_dep(route), (
            "POST /config/{section} must have require_elevation() — "
            "it covers the totp_elevation kill-switch config section."
        )

    def test_exempt_routes_accessible_without_elevation(self, client):
        """Routes that must stay ungated (GET logout, GET elevate form) do not
        have require_elevation in their dependencies.

        Gating logout creates a session-termination deadlock.
        Gating the elevation form creates a circular dependency where elevation
        is required to reach the elevation form.
        """
        from fastapi.routing import APIRoute

        from code_indexer.server.web import elevation_web_routes

        # GET /admin/logout must not be elevation-gated
        logout_route = _find_route("/logout", method="GET")
        assert logout_route is not None, "GET /logout not found in web_router"
        assert not _route_has_elevation_dep(logout_route), (
            "GET /logout must NOT have require_elevation — "
            "gating logout creates a session-termination deadlock."
        )

        # GET /admin/elevate lives in elevation_web_router (not web_router).
        # Verify GET specifically (not POST or any other method).
        elev_routes = [
            r
            for r in elevation_web_routes.router.routes
            if isinstance(r, APIRoute)
            and r.path == "/admin/elevate"
            and "GET" in (r.methods or set())
        ]
        assert elev_routes, (
            "GET /admin/elevate must be registered in elevation_web_router"
        )
        for route in elev_routes:
            assert not _route_has_elevation_dep(route), (
                "GET /admin/elevate must not require elevation — circular deadlock."
            )

        # HTTP smoke test: logout must redirect (3xx), not 403/503
        resp = cast(httpx.Response, client.get("/admin/logout"))
        assert resp.status_code in (301, 302, 303, 307, 308), (
            f"GET /admin/logout should redirect; got HTTP {resp.status_code}. "
            "Logout must remain accessible without elevation."
        )
