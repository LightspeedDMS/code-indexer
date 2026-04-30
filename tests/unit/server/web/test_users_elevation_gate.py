"""
Unit tests for elevation gate on users list partial endpoint.

Tests:
  GET /admin/partials/users-list - must have require_elevation() wired (structural gate)
  GET /admin/partials/users-list - returns 200 when elevation is active
  GET /admin/users               - shell page returns 200 without elevation
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


# Closure __qualname__ produced by require_elevation() factory — same identifier
# used in test_admin_elevation_gating_956.py for structural gating checks.
_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _route_has_elevation_dep(route) -> bool:
    """Return True if require_elevation() is wired to the given route.

    Mirrors the helper in test_admin_elevation_gating_956.py.
    """
    for dep in getattr(route, "dependencies", []):
        dep_callable = getattr(dep, "dependency", None)
        if dep_callable is None:
            continue
        if getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME:
            return True
    return False


def _bypass_elevation(app, router):
    """Override all require_elevation deps so elevated-path tests can run.

    Established project pattern used across multiple test files (e.g.
    test_dep_map_health_repair_api.py, test_groups_toggle_ajax.py).
    Required because CI has no TOTP setup; tests that verify route behaviour
    after elevation use this helper to simulate an active elevation window.
    """
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            dep_callable = getattr(dep, "dependency", None)
            if (
                dep_callable
                and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                app.dependency_overrides[dep_callable] = lambda: None


@pytest.fixture
def mock_session():
    """A mock admin SessionData object — avoids any login/credential flow."""
    session = MagicMock()
    session.username = "admin"
    session.role = "admin"
    return session


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def app(tmpdir_path):
    """Isolated app instance with a fresh temp DB — avoids locking the real DB.

    Imports are placed INSIDE the patch.dict block so that if code_indexer.server.app
    is first imported here, the module-level `app = create_app()` fires with the
    tmpdir env var active (no existing locked DB → fresh files created → no error).
    If already imported (cached from another test), the import is a no-op.
    """
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
        from code_indexer.server.app import create_app
        from code_indexer.server.services.config_service import reset_config_service
        from code_indexer.server.storage.database_manager import DatabaseSchema

        DatabaseSchema(str(tmpdir_path / "test.db")).initialize_database()
        reset_config_service()
        _app = create_app()
        original_overrides = dict(_app.dependency_overrides)
        yield _app
        _app.dependency_overrides = original_overrides
        reset_config_service()


@pytest.fixture
def client(app):
    """TestClient bound to the isolated app."""
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


class TestUsersListPartialElevationGate:
    """GET /admin/partials/users-list must be gated by require_elevation()."""

    def test_users_list_partial_requires_elevation(self):
        """Structural gate: require_elevation() must be wired to the route.

        Uses the same structural inspection approach as
        test_admin_elevation_gating_956.py::test_user_mutation_routes_require_elevation.
        No HTTP call required — inspects the FastAPI route descriptor directly.
        """
        from code_indexer.server.web.routes import web_router

        target = None
        for route in web_router.routes:
            if not isinstance(route, APIRoute):
                continue
            if route.path == "/partials/users-list" and "GET" in (route.methods or []):
                target = route
                break

        assert target is not None, (
            "GET /partials/users-list route not found in web_router"
        )
        assert _route_has_elevation_dep(target), (
            "GET /partials/users-list must have require_elevation() in its "
            "dependencies list"
        )

    def test_users_list_partial_allowed_when_elevated(
        self, app, client, mock_session
    ):
        """Admin with active elevation gets 200 from /admin/partials/users-list.

        _bypass_elevation simulates an active TOTP window (established pattern).
        _require_admin_session is patched to inject an admin session without
        a real login flow. auth_deps.user_manager is the external boundary
        behind _get_users_list and is patched at that boundary.
        """
        from code_indexer.server.web.routes import web_router
        from code_indexer.server.auth import dependencies as auth_deps

        _bypass_elevation(app, web_router)

        mock_user_manager = MagicMock()
        mock_user_manager.get_all_users.return_value = []

        with patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=mock_session,
        ):
            with patch.object(auth_deps, "user_manager", mock_user_manager):
                response = client.get("/admin/partials/users-list")

        assert response.status_code == 200, (
            f"Expected 200 with elevation, got {response.status_code}: {response.text}"
        )


class TestUsersPageShell:
    """GET /admin/users shell page must NOT require elevation."""

    def test_users_page_shell_does_not_require_elevation(
        self, client, mock_session
    ):
        """Shell /admin/users page renders without elevation.

        The user list no longer inlines via Jinja include — it loads via HTMX.
        The initial response must:
          - Return HTTP 200
          - Contain hx-trigger="load" on the deferred section
          - NOT contain the <h2>Users</h2> heading that users_list.html opens with,
            which would only appear if the partial were still inlined via
            Jinja {% include %} inside users-list-section.
        """
        with patch(
            "code_indexer.server.web.routes._require_admin_session",
            return_value=mock_session,
        ):
            response = client.get("/admin/users")

        assert response.status_code == 200, (
            f"Expected 200 for shell page, got {response.status_code}"
        )
        body = response.text
        assert 'hx-trigger="load"' in body, (
            "Shell page must contain hx-trigger='load' for deferred list loading"
        )
        assert "<h2>Users</h2>" not in body, (
            "Shell page must not inline the users_list.html partial content "
            "(which starts with <h2>Users</h2>) on initial render"
        )
