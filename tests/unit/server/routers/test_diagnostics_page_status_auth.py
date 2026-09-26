"""
Discriminating tests for admin authentication on the diagnostics page and status.

GET /admin/diagnostics and GET /admin/diagnostics/status must require admin
authentication; neither route may be reachable by an anonymous caller.

These tests exercise the routes through the real FastAPI dependency-injection
path (no mocked auth dependency for the anonymous cases) so they fail for the
right reason: an unauthenticated GET must be rejected, and an
authenticated admin must still be able to view diagnostics.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import code_indexer.server.web.auth as web_auth
from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.diagnostics import router


@pytest.fixture
def isolated_session_manager():
    """Initialize a real (throwaway) SessionManager for the duration of a test.

    _hybrid_auth_impl() unconditionally calls get_session_manager() before it
    even looks at the cookie, so an anonymous request needs a real manager
    wired up to reach the "no valid authentication found" 401 path instead of
    a RuntimeError.
    """
    original = web_auth._session_manager
    web_auth.init_session_manager(secret_key="test-secret-key", config=None)
    yield
    web_auth._session_manager = original


@pytest.fixture
def app(isolated_session_manager):
    """Isolated FastAPI app carrying only the diagnostics router."""
    fastapi_app = FastAPI()
    fastapi_app.include_router(router)
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_diagnostics_service():
    """Avoid touching the real on-disk diagnostics DB in this unit test."""
    with patch(
        "code_indexer.server.routers.diagnostics._get_diagnostics_service"
    ) as mock_getter:
        svc = MagicMock()
        svc.get_status.return_value = {}
        svc.is_running.return_value = False
        mock_getter.return_value = svc
        yield svc


@pytest.fixture(autouse=True)
def mock_csrf_cookie():
    """Patch set_csrf_cookie so the page route doesn't need a real session cookie flow."""
    with patch("code_indexer.server.routers.diagnostics.set_csrf_cookie"):
        yield


def _admin_user() -> User:
    return User(
        username="diagnostics-page-admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class TestDiagnosticsPageAnonymousRejected:
    """RED: anonymous callers must be rejected, not served real diagnostics HTML."""

    def test_get_diagnostics_page_anonymous_is_rejected(self, client):
        response = client.get("/admin/diagnostics")
        assert response.status_code == 401

    def test_get_diagnostics_status_anonymous_is_rejected(self, client):
        response = client.get("/admin/diagnostics/status")
        assert response.status_code == 401


def _non_admin_rejection() -> User:
    """Stand-in for get_current_admin_user_hybrid's real behavior when the
    caller is authenticated but lacks the admin role -- HTTPException(403,
    'Admin access required'), exactly as get_current_admin_user() raises.
    Mirrors test_global_config_git_settings_elevation.py's
    identical helper.
    """
    from fastapi import HTTPException, status

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
    )


class TestDiagnosticsPageNonAdminRejected:
    """A logged-in NON-admin must be rejected too,
    not just an anonymous caller -- proving the gate is admin auth, not
    merely "any authenticated user"."""

    def test_get_diagnostics_page_non_admin_is_rejected(self, app, client):
        app.dependency_overrides[get_current_admin_user_hybrid] = _non_admin_rejection
        response = client.get("/admin/diagnostics")
        assert response.status_code == 403

    def test_get_diagnostics_status_non_admin_is_rejected(self, app, client):
        app.dependency_overrides[get_current_admin_user_hybrid] = _non_admin_rejection
        response = client.get("/admin/diagnostics/status")
        assert response.status_code == 403


class TestDiagnosticsPageAdminAllowed:
    """GREEN: an authenticated admin can still reach both routes."""

    def test_get_diagnostics_page_admin_succeeds(self, app, client):
        app.dependency_overrides[get_current_admin_user_hybrid] = _admin_user
        response = client.get("/admin/diagnostics")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_get_diagnostics_status_admin_succeeds(self, app, client):
        app.dependency_overrides[get_current_admin_user_hybrid] = _admin_user
        response = client.get("/admin/diagnostics/status")
        assert response.status_code == 200
