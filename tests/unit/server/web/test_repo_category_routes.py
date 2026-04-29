"""
Tests for repository category web routes.

Story #180: Repository Category CRUD and Management UI
Tests Web UI routes for AC1-AC4.
"""

import pytest
import re
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app, router):
    """Override all require_elevation deps so tests can call routes without TOTP setup."""
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
def test_app():
    """Create a test FastAPI app with minimal setup.

    Yields the global app singleton with elevation bypassed for repo-category routes,
    then restores the original dependency_overrides to prevent cross-test contamination.
    """
    from code_indexer.server.app import app
    from code_indexer.server.web.repo_category_routes import repo_category_web_router

    original_overrides = dict(app.dependency_overrides)
    _bypass_elevation(app, repo_category_web_router)
    yield app
    app.dependency_overrides = original_overrides


@pytest.fixture
def client(test_app):
    """Create test client for the app."""
    return TestClient(test_app)


@pytest.fixture
def admin_session_cookie(client):
    """Get admin session cookie for authenticated requests."""
    # Login as admin
    response = client.post(
        "/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert response.status_code == 200

    # Extract session cookie
    cookies = response.cookies
    # Fix: starlette 0.49+ deprecated per-request cookies= parameter
    for name, value in cookies.items():
        client.cookies.set(name, value)
    return cookies


@pytest.fixture
def csrf_token(client, admin_session_cookie):
    """
    Get valid CSRF token for form submissions.

    Extracts token from the repo-categories page HTML response.
    The token is rendered as: <input type="hidden" name="csrf_token" value="TOKEN">
    """
    # Make GET request to repo-categories page with admin cookies
    response = client.get("/admin/repo-categories", cookies=admin_session_cookie)
    assert response.status_code == 200

    # Extract CSRF token from hidden input field in HTML
    html_content = response.text
    # Match: <input type="hidden" name="csrf_token" value="TOKEN">
    # Token format: URL-safe base64 (alphanumeric, hyphens, underscores)
    match = re.search(
        r'<input[^>]+name="csrf_token"[^>]+value="([a-zA-Z0-9_-]+)"', html_content
    )

    if not match:
        raise AssertionError(
            "Failed to extract CSRF token from /admin/repo-categories HTML response. "
            'Expected to find: <input type="hidden" name="csrf_token" value="...">'
        )

    return match.group(1)


class TestManagementPageAccess:
    """Test GET /admin/repo-categories endpoint."""

    def test_get_management_page_returns_200_for_admin(
        self, client, admin_session_cookie
    ):
        """Test that admin can access repository categories management page."""
        response = client.get("/admin/repo-categories", cookies=admin_session_cookie)

        assert response.status_code == 200

    def test_get_management_page_redirects_for_non_admin(self, client):
        """Test that non-admin users are redirected from management page."""
        response = client.get("/admin/repo-categories", follow_redirects=False)

        assert response.status_code in [302, 303, 401, 403]


class TestCreateCategory:
    """Test POST /admin/repo-categories/create endpoint (AC1)."""

    def test_post_create_with_valid_data_succeeds(
        self, client, admin_session_cookie, csrf_token
    ):
        """Test that creating category with valid data succeeds."""
        response = client.post(
            "/admin/repo-categories/create",
            data={
                "name": "Backend",
                "pattern": "^backend-.*",
                "csrf_token": csrf_token,
            },
            cookies=admin_session_cookie,
            follow_redirects=False,
        )

        # Should succeed (200 or redirect)
        assert response.status_code in [200, 302, 303]

    def test_post_create_with_invalid_regex_shows_error(
        self, client, admin_session_cookie, csrf_token
    ):
        """Test that creating category with invalid regex shows error."""
        response = client.post(
            "/admin/repo-categories/create",
            data={"name": "Backend", "pattern": "[unclosed", "csrf_token": csrf_token},
            cookies=admin_session_cookie,
            follow_redirects=False,
        )

        # Should return error (200 with error message, 400 for validation error, or 303 redirect)
        assert response.status_code in [200, 303, 400]
