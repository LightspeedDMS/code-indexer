"""
API endpoint tests for Dependency Map health and repair endpoints (Story #342).

Tests:
  GET  /admin/dependency-map/health  - returns structured JSON health report
  POST /admin/dependency-map/repair  - triggers background repair, returns 202

Test strategy:
  Uses real FastAPI test client with real app (same pattern as test_dependency_map_routes.py).
  Admin session obtained via form-based login (CSRF token flow).
  Mocking used only at the service layer boundary (_get_dep_map_output_dir,
  _get_dep_map_service_from_state) to isolate endpoint logic from filesystem/state.

All 5 tests map to Story #342 AC requirements.
"""

import re
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Elevation bypass helper (same pattern as test_repo_category_routes.py)
# ─────────────────────────────────────────────────────────────────────────────

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app, router):
    """Override all require_elevation deps so tests can call routes without TOTP setup.

    Necessary because the dev/CI database may have elevation_enforcement_enabled=True,
    which causes all elevation-gated endpoints to return 403 without an active TOTP
    window. Tests that are not testing elevation itself must bypass this gate.
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


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (same pattern as test_dependency_map_routes.py)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app():
    """Create FastAPI app with elevation bypassed for dependency-map routes.

    Restores original dependency_overrides after each test to prevent
    cross-test contamination.
    """
    from code_indexer.server.app import app as _app
    from code_indexer.server.web.dependency_map_routes import dependency_map_router

    original_overrides = dict(_app.dependency_overrides)
    _bypass_elevation(_app, dependency_map_router)
    yield _app
    _app.dependency_overrides = original_overrides


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_session_cookie(client):
    """Get admin session cookie via form-based login.

    The /auth/login JSON endpoint returns a JWT token but sets NO session
    cookies. Web routes use session-based auth via session_manager, so the
    correct auth path is POST /login with form data after extracting the
    CSRF token from GET /login.
    """
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

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

    # Fix: starlette 0.49+ deprecated per-request cookies= parameter
    for name, value in login_resp.cookies.items():
        client.cookies.set(name, value)
    return login_resp.cookies


# ─────────────────────────────────────────────────────────────────────────────
# GET /admin/dependency-map/health
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthEndpoint:
    """GET /admin/dependency-map/health returns structured health JSON."""

    def test_health_returns_structured_json(
        self, client, admin_session_cookie, tmp_path
    ):
        """Health endpoint returns JSON with status, anomalies, and repairable_count."""
        from tests.unit.server.services.test_dep_map_health_detector import (
            make_healthy_output_dir,
        )

        make_healthy_output_dir(tmp_path)

        with patch(
            "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
            return_value=tmp_path,
        ):
            response = client.get(
                "/admin/dependency-map/health",
                cookies=admin_session_cookie,
            )

        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")
        data = response.json()
        assert "status" in data, f"Missing 'status' in response: {data}"
        assert "anomalies" in data, f"Missing 'anomalies' in response: {data}"
        assert "repairable_count" in data, (
            f"Missing 'repairable_count' in response: {data}"
        )

    def test_health_requires_admin_auth(self, client):
        """GET /admin/dependency-map/health returns auth rejection without session."""
        response = client.get(
            "/admin/dependency-map/health",
            follow_redirects=False,
        )
        assert response.status_code in [
            302,
            303,
            401,
            403,
        ], f"Expected auth rejection, got {response.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# POST /admin/dependency-map/repair
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairEndpoint:
    """POST /admin/dependency-map/repair triggers background repair."""

    @pytest.fixture(autouse=True)
    def elevation_bypass(self, app):
        """Bypass elevation check for all repair endpoint tests."""
        from code_indexer.server.web.dependency_map_routes import dependency_map_router

        _bypass_elevation(app, dependency_map_router)
        yield
        for key in list(app.dependency_overrides.keys()):
            if getattr(key, "__qualname__", "") == _ELEVATION_QUALNAME:
                del app.dependency_overrides[key]

    def test_repair_returns_202_with_service_available(
        self, client, admin_session_cookie, tmp_path
    ):
        """Repair endpoint returns 202 Accepted when service is available."""
        from tests.unit.server.services.test_dep_map_health_detector import (
            make_healthy_output_dir,
        )

        make_healthy_output_dir(tmp_path)

        mock_service = MagicMock()
        mock_service.is_available.return_value = True

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_service_from_state",
                return_value=mock_service,
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
                return_value=tmp_path,
            ),
        ):
            response = client.post(
                "/admin/dependency-map/repair",
                cookies=admin_session_cookie,
            )

        assert response.status_code == 202
        assert "application/json" in response.headers.get("content-type", "")
        data = response.json()
        assert data.get("success") is True

    def test_repair_requires_admin_auth(self, client):
        """POST /admin/dependency-map/repair returns auth rejection without session."""
        response = client.post(
            "/admin/dependency-map/repair",
            follow_redirects=False,
        )
        assert response.status_code in [
            302,
            303,
            401,
            403,
        ], f"Expected auth rejection, got {response.status_code}"

    def test_repair_rejects_when_analysis_running(
        self, client, admin_session_cookie, tmp_path
    ):
        """Repair endpoint returns 409 when analysis is already in progress."""
        from tests.unit.server.services.test_dep_map_health_detector import (
            make_healthy_output_dir,
        )

        make_healthy_output_dir(tmp_path)

        # is_available() returns False when analysis is running
        mock_service = MagicMock()
        mock_service.is_available.return_value = False

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_service_from_state",
                return_value=mock_service,
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
                return_value=tmp_path,
            ),
        ):
            response = client.post(
                "/admin/dependency-map/repair",
                cookies=admin_session_cookie,
            )

        assert response.status_code == 409
        data = response.json()
        assert "error" in data


# ─────────────────────────────────────────────────────────────────────────────
# Story #342: Change 1 - Anomaly details passed to job-status partial template
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.slow
class TestJobStatusPartialAnomalyDetails:
    """
    Change 1: depmap_job_status_partial passes content_anomalies list to template.

    When content health check detects anomalies, the route must pass a
    content_anomalies list (not just a count) so the template can render
    human-readable descriptions of each anomaly.
    """

    def test_anomaly_details_rendered_in_partial_when_anomalies_exist(
        self, client, admin_session_cookie, tmp_path
    ):
        """Partial HTML contains anomaly detail text when anomalies are detected."""
        from tests.unit.server.services.test_dep_map_health_detector import (
            make_domains_json,
            make_domain_file,
            make_index_md,
        )

        # Create a dep map dir with an incomplete domain (missing required sections)
        # This produces an "incomplete_domain" anomaly
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "auth-domain",
                    "description": "Auth",
                    "participating_repos": ["repo-a"],
                }
            ],
        )
        incomplete_content = "x" * 1200  # big enough to pass size check but no sections
        make_domain_file(tmp_path, "auth-domain", content=incomplete_content)
        make_index_md(tmp_path)

        mock_cache = MagicMock()
        mock_cache.is_fresh.return_value = True
        mock_cache.get_cached.return_value = {"result_json": '{"status": "completed"}'}

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
                return_value=tmp_path,
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_known_repo_names",
                return_value=None,
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dashboard_cache_backend",
                return_value=mock_cache,
            ),
        ):
            response = client.get(
                "/admin/partials/depmap-job-status",
                cookies=admin_session_cookie,
            )

        assert response.status_code == 200
        # The rendered HTML must contain domain name from anomaly detail
        assert "auth-domain" in response.text, (
            f"Expected anomaly detail with 'auth-domain' in partial HTML. "
            f"Got: {response.text[500:1200]}"
        )

    def test_anomaly_details_absent_in_partial_when_healthy(
        self, client, admin_session_cookie, tmp_path
    ):
        """Partial HTML does not render anomaly detail list when dep map is healthy."""
        from tests.unit.server.services.test_dep_map_health_detector import (
            make_healthy_output_dir,
        )

        make_healthy_output_dir(tmp_path)

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
                return_value=tmp_path,
            ),
            patch(
                "code_indexer.server.web.dependency_map_routes._get_known_repo_names",
                return_value=None,
            ),
        ):
            response = client.get(
                "/admin/partials/depmap-job-status",
                cookies=admin_session_cookie,
            )

        assert response.status_code == 200
        # When healthy, no anomaly detail list should appear
        assert "Incomplete domain" not in response.text
        assert "Missing file" not in response.text
        assert "Uncovered repos" not in response.text

    def test_all_anomaly_types_produce_detail_text(
        self, client, admin_session_cookie, tmp_path
    ):
        """Partial renders a human-readable description for uncovered_repo anomaly."""
        from tests.unit.server.services.test_dep_map_health_detector import (
            make_healthy_output_dir,
        )

        make_healthy_output_dir(tmp_path)

        with (
            patch(
                "code_indexer.server.web.dependency_map_routes._get_dep_map_output_dir",
                return_value=tmp_path,
            ),
            patch(
                # Inject a known repo that is not covered by any domain
                "code_indexer.server.web.dependency_map_routes._get_known_repo_names",
                return_value={"repo-alpha", "repo-beta", "uncovered-service"},
            ),
        ):
            response = client.get(
                "/admin/partials/depmap-job-status",
                cookies=admin_session_cookie,
            )

        assert response.status_code == 200
        # uncovered_repo anomaly should produce "Uncovered repos: uncovered-service"
        assert "Uncovered repos" in response.text, (
            f"Expected 'Uncovered repos' in HTML. Got: {response.text[500:1200]}"
        )
        assert "uncovered-service" in response.text, (
            f"Expected repo name 'uncovered-service' in HTML. Got: {response.text[500:1200]}"
        )
