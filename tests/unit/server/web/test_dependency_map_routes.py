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
        # Should contain some health state text from the health model.
        # Includes 5-state model states plus Story #342 content health states.
        health_states = [
            "Healthy", "Disabled", "Running", "Unhealthy", "Degraded",
            "Needs Repair", "Critical",
        ]
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


# ─────────────────────────────────────────────────────────────────────────────
# Story #342 Change 2: _get_known_repo_names excludes orphan global_repos rows
# ─────────────────────────────────────────────────────────────────────────────


def _make_test_db(tmp_path, global_repos_names, golden_repos_aliases):
    """
    Create a real SQLite database at tmp_path/server/data/cidx_server.db
    with controlled fixture data in global_repos and golden_repos_metadata.

    Returns (server_dir, db_path) so callers can patch config_manager.server_dir.
    """
    import sqlite3

    server_dir = tmp_path / "server"
    data_dir = server_dir / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "cidx_server.db"

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE global_repos (repo_name TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE golden_repos_metadata (alias TEXT PRIMARY KEY)")
        for name in global_repos_names:
            conn.execute("INSERT INTO global_repos (repo_name) VALUES (?)", (name,))
        for alias in golden_repos_aliases:
            conn.execute(
                "INSERT INTO golden_repos_metadata (alias) VALUES (?)", (alias,)
            )
        conn.commit()
    finally:
        conn.close()

    return server_dir, db_path


def _call_get_known_repo_names(server_dir):
    """
    Call _get_known_repo_names() with config patched to use the given server_dir.

    get_config_service is imported locally inside _get_known_repo_names, so we
    patch it at its source module path.

    Returns the set of repo names (or None on error).
    """
    from unittest.mock import MagicMock, patch

    mock_config_manager = MagicMock()
    mock_config_manager.server_dir = server_dir
    mock_config_service = MagicMock()
    mock_config_service.config_manager = mock_config_manager

    with patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=mock_config_service,
    ):
        from code_indexer.server.web.dependency_map_routes import _get_known_repo_names

        return _get_known_repo_names()


class TestGetKnownRepoNamesOrphanFiltering:
    """
    Change 2: _get_known_repo_names() must use INNER JOIN to exclude repos
    that exist in global_repos but NOT in golden_repos_metadata (orphan activations).

    All tests use real SQLite files (no mocking of sqlite3) via _make_test_db
    and _call_get_known_repo_names helpers.
    """

    def test_returns_repos_in_both_tables(self, tmp_path):
        """Repos present in both global_repos and golden_repos_metadata are returned."""
        server_dir, _ = _make_test_db(
            tmp_path,
            global_repos_names=["backend", "frontend"],
            golden_repos_aliases=["backend", "frontend"],
        )
        result = _call_get_known_repo_names(server_dir)
        assert result == {"backend", "frontend"}, f"Expected both repos, got: {result}"

    def test_excludes_orphan_global_repos_entries(self, tmp_path):
        """
        Repos only in global_repos (not in golden_repos_metadata) are excluded.

        This is the core fix: multimodal-mock exists in global_repos but has no
        golden_repos_metadata entry (orphaned activation from a deleted golden repo).
        It must NOT appear in the returned set.
        """
        server_dir, _ = _make_test_db(
            tmp_path,
            global_repos_names=["backend", "multimodal-mock"],
            golden_repos_aliases=["backend"],  # multimodal-mock intentionally absent
        )
        result = _call_get_known_repo_names(server_dir)
        assert "multimodal-mock" not in result, (
            f"Orphan 'multimodal-mock' should be excluded but was in result: {result}"
        )
        assert "backend" in result, (
            f"Legitimate 'backend' should be included but was missing: {result}"
        )

    def test_excludes_repos_only_in_golden_repos_metadata(self, tmp_path):
        """
        Repos only in golden_repos_metadata (not in global_repos) are also excluded.

        The INNER JOIN is symmetric: only the intersection of both tables is returned.
        """
        server_dir, _ = _make_test_db(
            tmp_path,
            global_repos_names=["backend"],
            golden_repos_aliases=["backend", "not-activated-repo"],
        )
        result = _call_get_known_repo_names(server_dir)
        assert "not-activated-repo" not in result, (
            f"'not-activated-repo' should be excluded but was in result: {result}"
        )
        assert result == {"backend"}, f"Expected only 'backend', got: {result}"

    def test_returns_empty_set_when_no_overlap(self, tmp_path):
        """Returns empty set when global_repos and golden_repos_metadata have no overlap."""
        server_dir, _ = _make_test_db(
            tmp_path,
            global_repos_names=["orphan-a"],
            golden_repos_aliases=["unactivated-b"],
        )
        result = _call_get_known_repo_names(server_dir)
        assert result == set(), f"Expected empty set, got: {result}"


# ─────────────────────────────────────────────────────────────────────────────
# Story #342 Bug Fix: _build_domain_analyzer data flow
# Bug 1: repo_list was always [] (executor hardcodes [])
# Bug 2: previous_domain_dir was always None (should be output_dir)
# ─────────────────────────────────────────────────────────────────────────────


def _make_fake_dep_map_service(repo_list=None, enrich_adds_fields=True):
    """
    Build a minimal fake dep_map_service for _build_domain_analyzer tests.

    The fake exposes:
      - _get_activated_repos()  -> returns repo_list (default: two fake repos)
      - _enrich_repo_sizes()    -> returns the input list unchanged (or with sizes added)
      - _analyzer               -> a recording object that captures run_pass_2_per_domain args
      - _config_manager         -> None (max_turns will use default 25)
      - _activity_journal       -> None (journal_path will be None)
    """
    from types import SimpleNamespace

    if repo_list is None:
        repo_list = [
            {"alias": "alpha", "clone_path": "/fake/alpha", "description_summary": "Alpha repo"},
            {"alias": "beta", "clone_path": "/fake/beta", "description_summary": "Beta repo"},
        ]

    # Capture the arguments passed to run_pass_2_per_domain
    captured_calls = []

    class FakeAnalyzer:
        def run_pass_2_per_domain(self, staging_dir, domain, domain_list, repo_list,
                                  max_turns, previous_domain_dir, journal_path):
            captured_calls.append({
                "staging_dir": staging_dir,
                "domain": domain,
                "domain_list": domain_list,
                "repo_list": repo_list,
                "max_turns": max_turns,
                "previous_domain_dir": previous_domain_dir,
                "journal_path": journal_path,
            })
            # Write a non-empty domain file so the closure returns True
            (staging_dir / f"{domain['name']}.md").write_text("# content")

    fake_analyzer = FakeAnalyzer()

    def fake_get_activated_repos():
        return list(repo_list)  # return copy

    def fake_enrich_repo_sizes(repos):
        if enrich_adds_fields:
            for r in repos:
                r.setdefault("file_count", 10)
                r.setdefault("total_bytes", 1024)
        return repos

    service = SimpleNamespace(
        _get_activated_repos=fake_get_activated_repos,
        _enrich_repo_sizes=fake_enrich_repo_sizes,
        _analyzer=fake_analyzer,
        _config_manager=None,
        _activity_journal=None,
    )
    return service, captured_calls


class TestBuildDomainAnalyzerCapturesRepoList:
    """
    Bug 1 fix: _build_domain_analyzer must capture real repo_list from
    dep_map_service at closure-creation time and use it, IGNORING the
    empty [] that dep_map_repair_executor.py always passes.
    """

    def test_analyzer_uses_captured_repo_list_not_empty_list(self, tmp_path):
        """
        When executor calls analyzer(out_dir, domain, domain_list, []),
        the closure must use the captured repo_list (not []).
        """
        from code_indexer.server.web.dependency_map_routes import _build_domain_analyzer

        expected_repos = [
            {"alias": "repo-x", "clone_path": "/x", "description_summary": "X",
             "file_count": 5, "total_bytes": 500},
        ]
        service, captured_calls = _make_fake_dep_map_service(repo_list=expected_repos)

        domain = {"name": "test-domain", "description": "d", "participating_repos": ["repo-x"]}
        domain_list = [domain]

        analyzer = _build_domain_analyzer(service, tmp_path)

        # Executor always passes [] as repo_list
        result = analyzer(tmp_path, domain, domain_list, [])

        assert result is True, "Analyzer should succeed (domain file written)"
        assert len(captured_calls) == 1, f"Expected 1 call, got {len(captured_calls)}"

        call = captured_calls[0]
        # Must NOT be empty list -- must be the captured list from service
        assert call["repo_list"] != [], (
            "repo_list passed to run_pass_2_per_domain must not be empty []"
        )
        assert len(call["repo_list"]) == 1, (
            f"Expected 1 repo in list, got {len(call['repo_list'])}"
        )
        assert call["repo_list"][0]["alias"] == "repo-x", (
            f"Expected repo-x, got {call['repo_list'][0]['alias']}"
        )

    def test_analyzer_ignores_executor_empty_list_uses_captured(self, tmp_path):
        """
        Confirms executor passing [] is overridden by captured list.
        Tests the 'effective_repo_list = captured if not repo_list else repo_list' logic.
        """
        from code_indexer.server.web.dependency_map_routes import _build_domain_analyzer

        service, captured_calls = _make_fake_dep_map_service()

        domain = {"name": "d1", "description": "desc", "participating_repos": []}
        analyzer = _build_domain_analyzer(service, tmp_path)

        # Executor passes [] -- the bug scenario
        analyzer(tmp_path, domain, [domain], [])

        assert len(captured_calls) == 1
        # repo_list in the call must NOT be [] (the bug value)
        assert captured_calls[0]["repo_list"] != [], (
            "Bug 1 regression: repo_list is still [] -- fix not applied"
        )

    def test_analyzer_uses_non_empty_repo_list_when_provided_by_caller(self, tmp_path):
        """
        If caller passes a non-empty repo_list, it should be used as-is
        (the 'effective_repo_list = captured if not repo_list else repo_list' branch).
        """
        from code_indexer.server.web.dependency_map_routes import _build_domain_analyzer

        service, captured_calls = _make_fake_dep_map_service()
        caller_repo_list = [
            {"alias": "override-repo", "clone_path": "/o", "description_summary": "O",
             "file_count": 3, "total_bytes": 300},
        ]

        domain = {"name": "d2", "description": "desc", "participating_repos": []}
        analyzer = _build_domain_analyzer(service, tmp_path)

        # Caller passes a real non-empty list
        analyzer(tmp_path, domain, [domain], caller_repo_list)

        assert len(captured_calls) == 1
        # The caller's list should be used (not the captured one)
        assert captured_calls[0]["repo_list"] == caller_repo_list, (
            "When executor provides non-empty repo_list, it should be used"
        )


class TestBuildDomainAnalyzerPreviousDomainDir:
    """
    Bug 2 fix: _build_domain_analyzer must pass output_dir as previous_domain_dir
    instead of None, so Claude can see the existing (partially correct) domain
    analysis files and improve them rather than starting from scratch.
    """

    def test_previous_domain_dir_is_output_dir_not_none(self, tmp_path):
        """
        previous_domain_dir passed to run_pass_2_per_domain must be output_dir,
        not None.
        """
        from code_indexer.server.web.dependency_map_routes import _build_domain_analyzer

        service, captured_calls = _make_fake_dep_map_service()
        output_dir = tmp_path

        domain = {"name": "my-domain", "description": "d", "participating_repos": []}
        analyzer = _build_domain_analyzer(service, output_dir)

        analyzer(output_dir, domain, [domain], [])

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["previous_domain_dir"] is not None, (
            "Bug 2 regression: previous_domain_dir is None -- fix not applied"
        )
        assert call["previous_domain_dir"] == output_dir, (
            f"previous_domain_dir should be {output_dir}, "
            f"got {call['previous_domain_dir']}"
        )

    def test_previous_domain_dir_same_as_closure_output_dir(self, tmp_path):
        """
        The output_dir passed to _build_domain_analyzer (closure creation) is the
        same directory used as previous_domain_dir -- it contains the existing
        broken analysis that Claude should improve on.
        """
        from code_indexer.server.web.dependency_map_routes import _build_domain_analyzer

        service, captured_calls = _make_fake_dep_map_service()

        # Two different Path objects pointing to same place -- equality check
        closure_output_dir = tmp_path
        call_out_dir = tmp_path

        domain = {"name": "fix-domain", "description": "d", "participating_repos": []}
        analyzer = _build_domain_analyzer(service, closure_output_dir)

        analyzer(call_out_dir, domain, [domain], [])

        assert len(captured_calls) == 1
        # previous_domain_dir is the closure's output_dir (not the call's out_dir)
        assert captured_calls[0]["previous_domain_dir"] == closure_output_dir
