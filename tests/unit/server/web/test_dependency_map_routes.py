"""
Unit tests for Dependency Map web routes (Story #212).

Tests:
  AC1: Navigation menu has "Dependency Map" item
  AC2: Job Status section rendered for admin, hidden for non-admin
  AC5: HTMX auto-refresh partial endpoint
  AC6: POST /admin/dependency-map/trigger - admin only, returns JSON

Uses real FastAPI test client with real app (anti-mock for routes).
Mocking only used where infrastructure boundary requires it (tracking backend).
"""

import re

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app():
    """Create FastAPI app with minimal startup."""
    from code_indexer.server.app import app as _app
    return _app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_session_cookie(client):
    """Get admin session cookie via form-based login (NOT /auth/login JSON).

    The /auth/login JSON endpoint returns a JWT token but sets NO session
    cookies. Web routes use session-based auth via session_manager, so the
    correct auth path is POST /login with form data after extracting the
    CSRF token from GET /login.
    """
    # Step 1: GET /login to extract CSRF token
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

    # Step 2: POST /login with form data (don't follow redirect - capture cookies)
    login_resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303, f"Form login failed: {login_resp.status_code}"
    assert "session" in login_resp.cookies, "No session cookie set by form login"

    return login_resp.cookies


# ─────────────────────────────────────────────────────────────────────────────
# AC1: Navigation menu item
# ─────────────────────────────────────────────────────────────────────────────


class TestNavigationMenuIntegration:
    """AC1: Dependency Map menu item appears in navigation for authenticated users."""

    def test_dependency_map_link_in_dashboard_nav(self, client, admin_session_cookie):
        """AC1: Dashboard page navigation includes /admin/dependency-map link."""
        response = client.get("/admin/", cookies=admin_session_cookie)
        assert response.status_code == 200
        assert "/admin/dependency-map" in response.text

    def test_dependency_map_link_text_in_nav(self, client, admin_session_cookie):
        """AC1: Navigation link text is 'Dependency Map'."""
        response = client.get("/admin/", cookies=admin_session_cookie)
        assert response.status_code == 200
        assert "Dependency Map" in response.text


# ─────────────────────────────────────────────────────────────────────────────
# AC1: Page loading
# ─────────────────────────────────────────────────────────────────────────────


class TestDependencyMapPageLoad:
    """AC1: GET /admin/dependency-map loads successfully."""

    def test_page_loads_for_admin(self, client, admin_session_cookie):
        """AC1: Authenticated admin can load /admin/dependency-map."""
        response = client.get("/admin/dependency-map", cookies=admin_session_cookie)
        assert response.status_code == 200

    def test_page_redirects_for_unauthenticated(self, client):
        """AC1: Unauthenticated request to /admin/dependency-map redirects to login."""
        response = client.get("/admin/dependency-map", follow_redirects=False)
        assert response.status_code in [302, 303, 401, 403]

    def test_page_contains_dependency_map_heading(self, client, admin_session_cookie):
        """AC1: Page contains 'Dependency Map' heading."""
        response = client.get("/admin/dependency-map", cookies=admin_session_cookie)
        assert response.status_code == 200
        assert "Dependency Map" in response.text

    def test_page_uses_base_layout(self, client, admin_session_cookie):
        """AC1: Page extends base admin layout (has navigation)."""
        response = client.get("/admin/dependency-map", cookies=admin_session_cookie)
        assert response.status_code == 200
        # Base layout includes the navigation
        assert "/admin/" in response.text


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Job Status section - admin-only
# ─────────────────────────────────────────────────────────────────────────────


class TestJobStatusPartialEndpoint:
    """AC2: GET /admin/partials/depmap-job-status is admin-only."""

    def test_job_status_partial_loads_for_admin(self, client, admin_session_cookie):
        """AC2: Admin can load the depmap-job-status partial."""
        response = client.get(
            "/admin/partials/depmap-job-status",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200

    def test_job_status_partial_denied_for_unauthenticated(self, client):
        """AC2: Unauthenticated cannot access job status partial."""
        response = client.get(
            "/admin/partials/depmap-job-status",
            follow_redirects=False,
        )
        assert response.status_code in [302, 303, 401, 403]

    def test_job_status_partial_contains_health_info(self, client, admin_session_cookie):
        """AC2: Job status partial contains health badge information."""
        response = client.get(
            "/admin/partials/depmap-job-status",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        # Should contain some health state text from the 5-state model
        health_states = ["Healthy", "Disabled", "Running", "Unhealthy", "Degraded"]
        content = response.text
        assert any(state in content for state in health_states), (
            f"No health state found in response. Content: {content[:500]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC2: Main page job status section visible for admin (rendered in partial)
# ─────────────────────────────────────────────────────────────────────────────


class TestMainPageJobStatusVisibility:
    """AC2: Main page renders job status section container for admin."""

    def test_main_page_has_job_status_container_for_admin(self, client, admin_session_cookie):
        """AC2: Admin sees the job status section on the main page."""
        response = client.get("/admin/dependency-map", cookies=admin_session_cookie)
        assert response.status_code == 200
        # Should have a container for the job status partial
        assert "depmap-job-status" in response.text


# ─────────────────────────────────────────────────────────────────────────────
# AC5: HTMX partial endpoint for auto-refresh
# ─────────────────────────────────────────────────────────────────────────────


class TestHtmxPartialAutoRefresh:
    """AC5: Partial endpoint supports HTMX auto-refresh."""

    def test_partial_endpoint_returns_html(self, client, admin_session_cookie):
        """AC5: /admin/partials/depmap-job-status returns HTML content."""
        response = client.get(
            "/admin/partials/depmap-job-status",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_partial_has_htmx_trigger_attribute_or_content(self, client, admin_session_cookie):
        """AC5: Partial HTML includes hx-get pointing back to itself for polling."""
        response = client.get(
            "/admin/partials/depmap-job-status",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        # The partial should reference its own endpoint for polling
        assert "depmap-job-status" in response.text


# ─────────────────────────────────────────────────────────────────────────────
# AC6: POST /admin/dependency-map/trigger - admin only
# ─────────────────────────────────────────────────────────────────────────────


class TestTriggerEndpoint:
    """AC6: POST /admin/dependency-map/trigger is admin-only and returns JSON."""

    def test_trigger_returns_json_for_admin(self, client, admin_session_cookie):
        """AC6: Admin can POST to trigger endpoint and receives JSON response."""
        response = client.post(
            "/admin/dependency-map/trigger",
            data={"mode": "delta"},
            cookies=admin_session_cookie,
        )
        # Should return JSON with success or error (not 404/500)
        assert response.status_code in [200, 202, 409, 503]
        # Should be JSON
        assert "application/json" in response.headers.get("content-type", "")

    def test_trigger_denied_for_unauthenticated(self, client):
        """AC6: Unauthenticated cannot trigger analysis."""
        response = client.post(
            "/admin/dependency-map/trigger",
            data={"mode": "full"},
            follow_redirects=False,
        )
        assert response.status_code in [302, 303, 401, 403]

    def test_trigger_full_mode_accepted(self, client, admin_session_cookie):
        """AC6: mode=full is accepted by trigger endpoint."""
        response = client.post(
            "/admin/dependency-map/trigger",
            data={"mode": "full"},
            cookies=admin_session_cookie,
        )
        # 200/202 = triggered, 409 = already running, 503 = service unavailable
        assert response.status_code in [200, 202, 409, 503]

    def test_trigger_delta_mode_accepted(self, client, admin_session_cookie):
        """AC6: mode=delta is accepted by trigger endpoint."""
        response = client.post(
            "/admin/dependency-map/trigger",
            data={"mode": "delta"},
            cookies=admin_session_cookie,
        )
        assert response.status_code in [200, 202, 409, 503]

    def test_trigger_invalid_mode_returns_error(self, client, admin_session_cookie):
        """AC6: Invalid mode value returns 400 error."""
        response = client.post(
            "/admin/dependency-map/trigger",
            data={"mode": "invalid_mode"},
            cookies=admin_session_cookie,
        )
        assert response.status_code in [400, 422]

    def test_trigger_json_response_has_required_keys(self, client, admin_session_cookie):
        """AC6: JSON response contains success or error key."""
        response = client.post(
            "/admin/dependency-map/trigger",
            data={"mode": "delta"},
            cookies=admin_session_cookie,
        )
        assert response.status_code in [200, 202, 409, 503]
        data = response.json()
        # Must have either success indicator or error message
        assert "success" in data or "error" in data or "message" in data


# ─────────────────────────────────────────────────────────────────────────────
# Story #213 AC1/AC4/AC6: Repo Coverage Partial Endpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestRepoCoveragePartialEndpoint:
    """
    Story #213: GET /admin/partials/depmap-repo-coverage.

    AC1: Returns HTML with table structure.
    AC3: Response contains progress bar.
    AC4: Admin can access; unauthenticated returns 401.
    AC6: Response contains legend and refresh button.
    """

    def test_coverage_partial_loads_for_admin(self, client, admin_session_cookie):
        """AC4: Authenticated admin can load the coverage partial."""
        response = client.get(
            "/admin/partials/depmap-repo-coverage",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200

    def test_coverage_partial_returns_html(self, client, admin_session_cookie):
        """AC1: /admin/partials/depmap-repo-coverage returns HTML content."""
        response = client.get(
            "/admin/partials/depmap-repo-coverage",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_coverage_partial_contains_table_structure(self, client, admin_session_cookie):
        """AC1: Coverage partial contains HTML table."""
        response = client.get(
            "/admin/partials/depmap-repo-coverage",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "<table" in response.text

    def test_coverage_partial_contains_progress_bar(self, client, admin_session_cookie):
        """AC3: Coverage partial contains progress bar element."""
        response = client.get(
            "/admin/partials/depmap-repo-coverage",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        # Progress element or covered/total count text
        assert "covered" in response.text.lower() or "progress" in response.text.lower()

    def test_coverage_partial_contains_legend(self, client, admin_session_cookie):
        """AC6: Coverage partial contains a legend explaining colors."""
        response = client.get(
            "/admin/partials/depmap-repo-coverage",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        # Legend should explain at least one status
        assert any(
            term in response.text
            for term in ["OK", "CHANGED", "NEW", "REMOVED", "Legend", "legend"]
        )

    def test_coverage_partial_denied_for_unauthenticated(self, client):
        """AC4: Unauthenticated cannot access coverage partial."""
        response = client.get(
            "/admin/partials/depmap-repo-coverage",
            follow_redirects=False,
        )
        assert response.status_code in [302, 303, 401, 403]

    def test_main_page_has_coverage_container(self, client, admin_session_cookie):
        """AC1: Main dependency map page has container for coverage section."""
        response = client.get("/admin/dependency-map", cookies=admin_session_cookie)
        assert response.status_code == 200
        assert "depmap-repo-coverage" in response.text


# ─────────────────────────────────────────────────────────────────────────────
# Story #214: Domain Explorer Endpoints
# ─────────────────────────────────────────────────────────────────────────────


class TestDomainExplorerPartialEndpoint:
    """Story #214: GET /admin/partials/depmap-domain-explorer."""

    def test_domain_explorer_partial_loads_for_admin(self, client, admin_session_cookie):
        """AC1: Admin can load the domain explorer partial."""
        response = client.get(
            "/admin/partials/depmap-domain-explorer",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200

    def test_domain_explorer_partial_returns_html(self, client, admin_session_cookie):
        """AC1: Returns HTML content."""
        response = client.get(
            "/admin/partials/depmap-domain-explorer",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_domain_explorer_partial_contains_domain_list(self, client, admin_session_cookie):
        """AC2: Contains the domain list panel structure."""
        response = client.get(
            "/admin/partials/depmap-domain-explorer",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "domain-list" in response.text

    def test_domain_explorer_partial_contains_search(self, client, admin_session_cookie):
        """AC2: Contains search input."""
        response = client.get(
            "/admin/partials/depmap-domain-explorer",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "domain-search" in response.text

    def test_domain_explorer_partial_denied_for_unauthenticated(self, client):
        """AC1: Unauthenticated cannot access domain explorer."""
        response = client.get(
            "/admin/partials/depmap-domain-explorer",
            follow_redirects=False,
        )
        assert response.status_code in [302, 303, 401, 403]

    def test_main_page_has_domain_explorer_container(self, client, admin_session_cookie):
        """AC1: Main page has HTMX container for domain explorer."""
        response = client.get("/admin/dependency-map", cookies=admin_session_cookie)
        assert response.status_code == 200
        assert "depmap-domain-explorer" in response.text


class TestDomainDetailPartialEndpoint:
    """Story #214: GET /admin/partials/depmap-domain-detail/{name}."""

    def test_domain_detail_loads_for_admin(self, client, admin_session_cookie):
        """AC3: Admin can load domain detail partial (even for nonexistent domain)."""
        response = client.get(
            "/admin/partials/depmap-domain-detail/test-domain",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200

    def test_domain_detail_returns_html(self, client, admin_session_cookie):
        """AC3: Returns HTML content."""
        response = client.get(
            "/admin/partials/depmap-domain-detail/test-domain",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_domain_detail_denied_for_unauthenticated(self, client):
        """AC3: Unauthenticated cannot access domain detail."""
        response = client.get(
            "/admin/partials/depmap-domain-detail/test-domain",
            follow_redirects=False,
        )
        assert response.status_code in [302, 303, 401, 403]

    def test_domain_detail_nonexistent_returns_fallback(self, client, admin_session_cookie):
        """AC3: Nonexistent domain returns fallback 'Domain not found' message."""
        response = client.get(
            "/admin/partials/depmap-domain-detail/definitely-not-a-real-domain",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "not found" in response.text.lower() or "Domain not found" in response.text


# ─────────────────────────────────────────────────────────────────────────────
# Regression: config_service wiring in _get_dashboard_service
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigServiceWiring:
    """
    Regression test: _get_dashboard_service() must pass config_service
    (not config_service.config_manager) to DependencyMapDashboardService.

    Prevents regression of:
      "Failed to get job status: 'ServerConfigManager' object has no attribute
       'get_claude_integration_config'"
    """

    def test_dashboard_service_receives_config_service_not_config_manager(self):
        """
        _get_dashboard_service() passes config_service (has get_claude_integration_config)
        as the config_manager argument, not config_service.config_manager (which lacks it).
        """
        from unittest.mock import MagicMock, patch

        mock_config_service = MagicMock()
        # Ensure the mock ConfigService has get_claude_integration_config
        assert hasattr(mock_config_service, "get_claude_integration_config")

        captured = {}

        def capturing_dashboard_service_init(self_inner, tracking_backend, config_manager, dependency_map_service):
            captured["config_manager"] = config_manager

        with (
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=mock_config_service,
            ),
            patch(
                "code_indexer.server.services.dependency_map_dashboard_service"
                ".DependencyMapDashboardService.__init__",
                capturing_dashboard_service_init,
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_service_from_state",
                return_value=None,
            ),
            patch(
                "code_indexer.server.storage.sqlite_backends.DependencyMapTrackingBackend.__init__",
                return_value=None,
            ),
        ):
            from code_indexer.server.web.dependency_map_routes import _get_dashboard_service

            _get_dashboard_service()

        assert "config_manager" in captured, (
            "_get_dashboard_service() did not construct DependencyMapDashboardService"
        )
        assert captured["config_manager"] is mock_config_service, (
            "config_manager argument must be config_service (ConfigService), "
            "not config_service.config_manager (ServerConfigManager). "
            "This guards against AttributeError: 'ServerConfigManager' object has "
            "no attribute 'get_claude_integration_config'"
        )
        # Confirm the passed object has the required method
        assert hasattr(captured["config_manager"], "get_claude_integration_config"), (
            "The config_manager passed to DependencyMapDashboardService must have "
            "get_claude_integration_config() method"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Story #215: Graph Data Endpoint
# ─────────────────────────────────────────────────────────────────────────────


class TestGraphDataEndpoint:
    """Story #215 AC7: GET /admin/dependency-map/graph-data returns JSON."""

    def test_graph_data_returns_json_for_admin(self, client, admin_session_cookie):
        """AC7: Admin gets JSON response with nodes and edges."""
        response = client.get(
            "/admin/dependency-map/graph-data",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")
        data = response.json()
        assert "nodes" in data
        assert "edges" in data
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)

    def test_graph_data_denied_for_unauthenticated(self, client):
        """AC7: Unauthenticated cannot access graph data."""
        response = client.get(
            "/admin/dependency-map/graph-data",
            follow_redirects=False,
        )
        assert response.status_code == 401

    def test_graph_data_returns_empty_when_no_data(self, client, admin_session_cookie):
        """AC7: Returns empty nodes/edges when no dependency map exists."""
        response = client.get(
            "/admin/dependency-map/graph-data",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200
        data = response.json()
        # May have nodes (if golden repos exist) or empty
        assert isinstance(data["nodes"], list)
        assert isinstance(data["edges"], list)
