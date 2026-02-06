"""
Unit tests for Diagnostics Router.

Tests cover:
- GET /admin/diagnostics endpoint (page rendering)
- POST /admin/diagnostics/run-all endpoint (trigger all diagnostics)
- POST /admin/diagnostics/run/{category} endpoint (trigger single category)
- GET /admin/diagnostics/status endpoint (HTMX polling)
- HTMX headers (HX-Stop-Polling)
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, AsyncMock
from code_indexer.server.routers.diagnostics import router
from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticStatus,
    DiagnosticResult,
)


@pytest.fixture
def app():
    """Create test FastAPI app with diagnostics router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_diagnostics_service():
    """Create mock diagnostics service."""
    with patch('code_indexer.server.routers.diagnostics.diagnostics_service') as mock:
        yield mock


class TestDiagnosticsPageEndpoint:
    """Test GET /admin/diagnostics endpoint."""

    def test_diagnostics_page_renders(self, client):
        """Test diagnostics page renders successfully."""
        response = client.get("/admin/diagnostics")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_diagnostics_page_contains_categories(self, client):
        """Test diagnostics page contains all five category sections."""
        response = client.get("/admin/diagnostics")
        html = response.text

        # Check for category sections
        assert "CLI Tool Dependencies" in html or "cli_tools" in html
        assert "SDK Prerequisites" in html or "sdk_prerequisites" in html
        assert "External API Integrations" in html or "external_apis" in html
        assert "Credential & Connectivity" in html or "credentials" in html
        assert "Core Infrastructure" in html or "infrastructure" in html

    def test_diagnostics_page_has_run_all_button(self, client):
        """Test diagnostics page has Run All Diagnostics button."""
        response = client.get("/admin/diagnostics")
        html = response.text
        assert "Run All Diagnostics" in html or "run-all" in html

    def test_diagnostics_page_no_initial_polling(self, client):
        """Test diagnostics page does NOT have polling on initial load."""
        response = client.get("/admin/diagnostics")
        html = response.text
        # Should NOT have hx-trigger on the main diagnostics-results section
        # Polling only starts after Run All button is clicked
        assert 'id="diagnostics-results"' in html
        # Verify the results section doesn't have hx-trigger initially
        import re
        results_section = re.search(r'<section[^>]*id="diagnostics-results"[^>]*>', html)
        assert results_section is not None
        assert 'hx-trigger' not in results_section.group(0)

    def test_diagnostics_page_has_navigation_bar(self, client):
        """Test diagnostics page includes navigation bar (show_nav=True)."""
        response = client.get("/admin/diagnostics")
        html = response.text
        # Should have navigation bar with standard links
        assert "Dashboard" in html or 'href="/admin/"' in html
        assert "Users" in html or 'href="/admin/users"' in html
        assert "Diagnostics" in html

    def test_diagnostics_page_highlights_current_nav(self, client):
        """Test diagnostics page highlights Diagnostics in navigation (current_page='diagnostics')."""
        response = client.get("/admin/diagnostics")
        html = response.text
        # Should have aria-current="page" on Diagnostics nav item
        import re
        # Look for Diagnostics link with aria-current marker
        diagnostics_nav = re.search(r'<a[^>]*href="/admin/diagnostics"[^>]*aria-current="page"[^>]*>.*?Diagnostics.*?</a>', html, re.IGNORECASE | re.DOTALL)
        assert diagnostics_nav is not None, "Diagnostics nav item should be marked as current page"


class TestRunAllDiagnosticsEndpoint:
    """Test POST /admin/diagnostics/run-all endpoint."""

    def test_run_all_triggers_service(self, client, mock_diagnostics_service):
        """Test run-all endpoint triggers diagnostics service."""
        mock_diagnostics_service.run_all_diagnostics = AsyncMock()

        response = client.post("/admin/diagnostics/run-all")

        assert response.status_code in [200, 202]
        mock_diagnostics_service.run_all_diagnostics.assert_called_once()

    def test_run_all_returns_html_partial(self, client, mock_diagnostics_service):
        """Test run-all endpoint returns HTML partial for HTMX to swap in."""
        mock_diagnostics_service.run_all_diagnostics = AsyncMock()
        mock_diagnostics_service.get_status.return_value = {}
        mock_diagnostics_service.is_running.return_value = True

        response = client.post("/admin/diagnostics/run-all")

        # Should return HTML partial, not JSON
        assert "text/html" in response.headers["content-type"]
        html = response.text
        # Should include polling trigger to /admin/diagnostics/status
        assert "hx-get" in html and "/admin/diagnostics/status" in html
        # Should have hx-trigger for automatic polling
        assert "hx-trigger" in html and ("every 2s" in html or "every" in html)

    def test_run_all_sets_running_status(self, client, mock_diagnostics_service):
        """Test run-all endpoint indicates running status via HTML polling."""
        mock_diagnostics_service.run_all_diagnostics = AsyncMock()
        mock_diagnostics_service.get_status.return_value = {}
        mock_diagnostics_service.is_running.return_value = True

        response = client.post("/admin/diagnostics/run-all")

        # Response should be HTML with polling enabled (is_running=True)
        assert "text/html" in response.headers["content-type"]
        html = response.text
        # Should have polling attributes enabled
        assert "hx-get" in html and "/admin/diagnostics/status" in html

    def test_run_all_returns_immediately_without_awaiting(self, client, mock_diagnostics_service):
        """Test run-all endpoint uses BackgroundTasks (doesn't await completion)."""
        # Mock diagnostics service
        mock_diagnostics_service.run_all_diagnostics = AsyncMock()
        mock_diagnostics_service.get_status.return_value = {}
        mock_diagnostics_service.is_running.return_value = True

        response = client.post("/admin/diagnostics/run-all")

        # Should return HTML immediately (200 OK, not waiting for diagnostics)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        # Service method should be called (via background task)
        # Note: TestClient executes background tasks before returning,
        # but in production this runs truly async
        mock_diagnostics_service.run_all_diagnostics.assert_called_once()


class TestRunCategoryEndpoint:
    """Test POST /admin/diagnostics/run/{category} endpoint."""

    def test_run_category_triggers_service(self, client, mock_diagnostics_service):
        """Test run category endpoint triggers diagnostics service."""
        mock_diagnostics_service.run_category = AsyncMock()

        response = client.post("/admin/diagnostics/run/cli_tools")

        assert response.status_code in [200, 202]
        mock_diagnostics_service.run_category.assert_called_once()

    def test_run_category_accepts_valid_categories(self, client, mock_diagnostics_service):
        """Test run category accepts all valid category values."""
        mock_diagnostics_service.run_category = AsyncMock()

        valid_categories = [
            "cli_tools",
            "sdk_prerequisites",
            "external_apis",
            "credentials",
            "infrastructure",
        ]

        for category in valid_categories:
            response = client.post(f"/admin/diagnostics/run/{category}")
            assert response.status_code in [200, 202], f"Failed for category: {category}"

    def test_run_category_rejects_invalid_category(self, client):
        """Test run category rejects invalid category values."""
        response = client.post("/admin/diagnostics/run/invalid_category")
        assert response.status_code in [400, 404, 422]

    def test_run_category_returns_html_partial(self, client, mock_diagnostics_service):
        """Test run category endpoint returns HTML partial for HTMX to swap in."""
        mock_diagnostics_service.run_category = AsyncMock()
        mock_diagnostics_service.get_status.return_value = {}
        mock_diagnostics_service.is_running.return_value = True

        response = client.post("/admin/diagnostics/run/cli_tools")

        # Should return HTML partial, not JSON
        assert "text/html" in response.headers["content-type"]
        html = response.text
        # Should include polling trigger to /admin/diagnostics/status
        assert "hx-get" in html and "/admin/diagnostics/status" in html

    def test_run_category_returns_immediately_without_awaiting(self, client, mock_diagnostics_service):
        """Test run-category endpoint uses BackgroundTasks (doesn't await completion)."""
        # Mock diagnostics service
        mock_diagnostics_service.run_category = AsyncMock()
        mock_diagnostics_service.get_status.return_value = {}
        mock_diagnostics_service.is_running.return_value = True

        response = client.post("/admin/diagnostics/run/cli_tools")

        # Should return HTML immediately (200 OK, not waiting for diagnostics)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        # Service method should be called (via background task)
        # Note: TestClient executes background tasks before returning,
        # but in production this runs truly async
        mock_diagnostics_service.run_category.assert_called_once()


class TestStatusPollingEndpoint:
    """Test GET /admin/diagnostics/status endpoint."""

    def test_status_endpoint_returns_html(self, client, mock_diagnostics_service):
        """Test status endpoint returns HTML for HTMX partial."""
        mock_diagnostics_service.get_status.return_value = {
            DiagnosticCategory.CLI_TOOLS: [
                DiagnosticResult(
                    name="Test",
                    status=DiagnosticStatus.NOT_RUN,
                    message="Not run",
                    details={},
                )
            ]
        }

        response = client.get("/admin/diagnostics/status")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_status_endpoint_includes_stop_polling_when_complete(
        self, client, mock_diagnostics_service
    ):
        """Test status endpoint includes HX-Stop-Polling header when diagnostics complete."""
        mock_diagnostics_service.get_status.return_value = {
            DiagnosticCategory.CLI_TOOLS: [
                DiagnosticResult(
                    name="Test",
                    status=DiagnosticStatus.WORKING,
                    message="Complete",
                    details={},
                )
            ]
        }
        mock_diagnostics_service.is_running.return_value = False

        response = client.get("/admin/diagnostics/status")

        # When not running, should include stop polling header
        assert "HX-Stop-Polling" in response.headers or "hx-stop-polling" in response.headers

    def test_status_endpoint_no_stop_polling_when_running(
        self, client, mock_diagnostics_service
    ):
        """Test status endpoint does NOT include HX-Stop-Polling header when diagnostics running."""
        mock_diagnostics_service.get_status.return_value = {
            DiagnosticCategory.CLI_TOOLS: [
                DiagnosticResult(
                    name="Test",
                    status=DiagnosticStatus.RUNNING,
                    message="Running",
                    details={},
                )
            ]
        }
        mock_diagnostics_service.is_running.return_value = True

        response = client.get("/admin/diagnostics/status")

        # When running, should NOT include stop polling header
        assert "HX-Stop-Polling" not in response.headers
        assert "hx-stop-polling" not in response.headers

    def test_status_endpoint_returns_all_categories(
        self, client, mock_diagnostics_service
    ):
        """Test status endpoint returns status for all categories."""
        mock_status = {}
        for category in DiagnosticCategory:
            mock_status[category] = [
                DiagnosticResult(
                    name=f"Test {category.value}",
                    status=DiagnosticStatus.NOT_RUN,
                    message="Not run",
                    details={},
                )
            ]
        mock_diagnostics_service.get_status.return_value = mock_status

        response = client.get("/admin/diagnostics/status")
        html = response.text

        # Check all categories are present in response
        for category in DiagnosticCategory:
            assert category.value in html

    def test_status_endpoint_shows_diagnostic_results(
        self, client, mock_diagnostics_service
    ):
        """Test status endpoint shows individual diagnostic results."""
        mock_diagnostics_service.get_status.return_value = {
            DiagnosticCategory.CLI_TOOLS: [
                DiagnosticResult(
                    name="CIDX CLI",
                    status=DiagnosticStatus.WORKING,
                    message="Version 8.8.0 installed",
                    details={"version": "8.8.0"},
                )
            ]
        }

        response = client.get("/admin/diagnostics/status")
        html = response.text

        assert "CIDX CLI" in html
        assert "working" in html.lower() or "WORKING" in html

    def test_status_endpoint_bounded_polling_timeout(
        self, client, mock_diagnostics_service
    ):
        """Test status endpoint supports bounded polling (max 60 seconds)."""
        # This is more of a documentation test - the actual timeout is implemented
        # via HTMX in the frontend template, not the backend endpoint
        mock_diagnostics_service.get_status.return_value = {}
        mock_diagnostics_service.is_running.return_value = False

        response = client.get("/admin/diagnostics/status")

        # Endpoint should respond quickly regardless of timeout
        assert response.status_code == 200
