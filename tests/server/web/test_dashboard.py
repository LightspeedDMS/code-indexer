"""
Tests for System Dashboard (Story #531).

These tests follow TDD methodology - tests are written FIRST before implementation.
All tests use real components following MESSI Rule #1: No mocks.
"""

from fastapi.testclient import TestClient


# =============================================================================
# AC1: Dashboard Page Access Tests
# =============================================================================


class TestDashboardAccess:
    """Tests for dashboard page access (AC1)."""

    def test_dashboard_requires_auth(self, web_client: TestClient):
        """
        AC1: Unauthenticated access to /admin/ redirects to login.

        Given I am not authenticated
        When I navigate to /admin/
        Then I am redirected to /admin/login
        """
        response = web_client.get("/admin/")

        assert response.status_code in [
            302,
            303,
        ], f"Expected redirect, got {response.status_code}"
        location = response.headers.get("location", "")
        assert (
            "/admin/login" in location
        ), f"Expected redirect to /admin/login, got {location}"

    def test_dashboard_renders(self, authenticated_client: TestClient):
        """
        AC1: Authenticated admin access to /admin/ shows dashboard page.

        Given I am authenticated as an admin
        When I navigate to /admin/
        Then I see the dashboard page with title "Dashboard - CIDX Admin"
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        assert (
            "Dashboard - CIDX Admin" in response.text
        ), "Page title should be 'Dashboard - CIDX Admin'"

    def test_dashboard_nav_highlighted(self, authenticated_client: TestClient):
        """
        AC1: Dashboard is highlighted in navigation.

        Given I am authenticated as an admin
        When I view the dashboard
        Then the Dashboard link is highlighted in navigation
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        # The dashboard should have aria-current="page" attribute
        assert (
            'aria-current="page"' in response.text
        ), "Dashboard should be highlighted with aria-current attribute"


# =============================================================================
# AC2: System Health Display Tests
# =============================================================================


class TestSystemHealthDisplay:
    """Tests for system health display (AC2)."""

    def test_dashboard_shows_health_section(self, authenticated_client: TestClient):
        """
        AC2: Dashboard shows "System Health" section.

        Given I am on the dashboard
        When the page loads
        Then I see a "System Health" section
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        assert (
            "System Health" in response.text
        ), "Dashboard should contain 'System Health' section"

    def test_dashboard_shows_health_status(self, authenticated_client: TestClient):
        """
        AC2: Dashboard shows server, database, and vector store status.

        Given I am on the dashboard
        When the page loads
        Then I see status indicators for server, database, and vector store
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        # Check for health status indicators
        text_lower = response.text.lower()
        assert (
            "server" in text_lower or "api" in text_lower
        ), "Dashboard should show server status"
        # Check for status indicator classes
        assert (
            "status-indicator" in response.text or "status-" in response.text
        ), "Dashboard should have status indicator elements"


# =============================================================================
# AC3: Job Statistics Display Tests
# =============================================================================


class TestJobStatistics:
    """Tests for job statistics display (AC3)."""

    def test_dashboard_shows_job_counts(self, authenticated_client: TestClient):
        """
        AC3: Dashboard shows "Jobs" section with counts.

        Given I am on the dashboard
        When the page loads
        Then I see a "Jobs" section with running, queued, completed, and failed counts
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        text_lower = response.text.lower()
        assert "jobs" in text_lower, "Dashboard should contain 'Jobs' section"
        # Should have job-related count displays
        assert (
            "running" in text_lower or "queued" in text_lower
        ), "Dashboard should show job status counts"

    def test_job_counts_link_to_jobs_page(self, authenticated_client: TestClient):
        """
        AC3: Each job count links to the jobs page with appropriate filter.

        Given I am on the dashboard
        When I view the job statistics
        Then each count links to the jobs page
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        assert (
            "/admin/jobs" in response.text
        ), "Dashboard should have links to jobs page"


# =============================================================================
# AC4: Repository Statistics Display Tests
# =============================================================================


class TestRepositoryStatistics:
    """Tests for repository statistics display (AC4)."""

    def test_dashboard_shows_repo_counts(self, authenticated_client: TestClient):
        """
        AC4: Dashboard shows "Repositories" section with counts.

        Given I am on the dashboard
        When the page loads
        Then I see a "Repositories" section with golden repos, activated repos counts
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        text_lower = response.text.lower()
        assert (
            "repositories" in text_lower or "repos" in text_lower
        ), "Dashboard should contain 'Repositories' section"

    def test_repo_counts_link_to_management_pages(
        self, authenticated_client: TestClient
    ):
        """
        AC4: Each repo count links to the respective management page.

        Given I am on the dashboard
        When I view the repository statistics
        Then each count links to the respective management page
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        assert (
            "/admin/golden-repos" in response.text or "/admin/repos" in response.text
        ), "Dashboard should have links to repository management pages"


# =============================================================================
# AC5: Auto-Refresh Capability Tests
# =============================================================================


class TestAutoRefresh:
    """Tests for auto-refresh capability (AC5)."""

    def test_dashboard_auto_refresh_toggle(self, authenticated_client: TestClient):
        """
        AC5: Dashboard has toggle to enable/disable auto-refresh.

        Given I am on the dashboard
        When I view the page
        Then I see a toggle for auto-refresh
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        # Check for auto-refresh toggle element
        text_lower = response.text.lower()
        assert (
            "auto-refresh" in text_lower or "autorefresh" in text_lower
        ), "Dashboard should have auto-refresh toggle"


# =============================================================================
# AC6: Manual Refresh Tests
# =============================================================================


class TestManualRefresh:
    """Tests for manual refresh (AC6)."""

    def test_dashboard_refresh_button(self, authenticated_client: TestClient):
        """
        AC6: Dashboard has a "Refresh" button for manual refresh.

        Given I am on the dashboard
        When I view the page
        Then I see a refresh button
        And the button uses htmx for partial refresh
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        text_lower = response.text.lower()
        assert "refresh" in text_lower, "Dashboard should have refresh button"
        # Should use htmx for refresh
        assert (
            "hx-get" in response.text or "hx-post" in response.text
        ), "Refresh should use htmx attributes"


# =============================================================================
# AC7: Recent Activity Summary Tests
# =============================================================================


class TestRecentActivity:
    """Tests for recent activity summary (AC7)."""

    def test_dashboard_shows_recent_activity(self, authenticated_client: TestClient):
        """
        AC7: Dashboard shows "Recent Activity" section.

        Given I am on the dashboard
        When the page loads
        Then I see a "Recent Activity" section
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        text_lower = response.text.lower()
        assert (
            "recent" in text_lower or "activity" in text_lower
        ), "Dashboard should contain 'Recent Activity' section"


# =============================================================================
# Story #201: Recent Activity No Dropdown Tests
# =============================================================================


class TestRecentActivityNoDropdown:
    """Tests for Story #201: Remove Recent Activity time range dropdown."""

    def test_no_recent_filter_dropdown(self, authenticated_client: TestClient):
        """
        Story #201 AC1: No time range dropdown in Recent Activity section.

        Given I am on the dashboard
        When I view the Recent Activity section
        Then I do NOT see a time range dropdown with id="recent-filter"
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        # Should NOT have recent-filter dropdown
        assert (
            'id="recent-filter"' not in response.text
        ), "Recent Activity should not have a time range dropdown"

    def test_recent_activity_plain_header(self, authenticated_client: TestClient):
        """
        Story #201 AC1: Recent Activity section shows plain header without dropdown.

        Given I am on the dashboard
        When I view the Recent Activity section
        Then I see "Recent Activity" header without any filter controls
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        assert "Recent Activity" in response.text
        # Should NOT have the dropdown structure
        assert 'id="recent-filter"' not in response.text

    def test_recent_jobs_defaults_to_24h(self, authenticated_client: TestClient):
        """
        Story #201 AC2: Recent Activity shows last 24 hours of data by default.

        Given I am on the dashboard
        When the page loads
        Then Recent Activity displays jobs from the last 24 hours
        """
        # The partial endpoint should default to 24h
        response = authenticated_client.get("/admin/partials/dashboard-recent-jobs")

        assert response.status_code == 200
        # Should succeed (implicit 24h default)

    def test_no_update_recent_activity_function(self, authenticated_client: TestClient):
        """
        Story #201 AC3: updateRecentActivity() JS function removed.

        Given I am on the dashboard
        When I view the page source
        Then I do NOT see updateRecentActivity() function
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        assert (
            "updateRecentActivity" not in response.text
        ), "updateRecentActivity() function should be removed"

    def test_other_dropdowns_still_exist(self, authenticated_client: TestClient):
        """
        Story #201 AC4: Other dashboard dropdowns (Job Stats, API Activity) still function.

        Given I am on the dashboard
        When I view the page
        Then I see time-filter dropdown (Job Statistics)
        And I see api-filter dropdown (API Activity)
        """
        response = authenticated_client.get("/admin/")

        assert response.status_code == 200
        # Job Statistics dropdown should still exist
        assert (
            'id="time-filter"' in response.text
        ), "Job Statistics time filter dropdown should still exist"
        # API Activity dropdown should still exist
        assert (
            'id="api-filter"' in response.text
        ), "API Activity time filter dropdown should still exist"
        # Recent Activity dropdown should NOT exist
        assert (
            'id="recent-filter"' not in response.text
        ), "Recent Activity dropdown should be removed"


# =============================================================================
# Partial Refresh Endpoint Tests
# =============================================================================


class TestDashboardPartials:
    """Tests for htmx partial refresh endpoints."""

    def test_dashboard_partial_health(self, authenticated_client: TestClient):
        """
        GET /admin/partials/dashboard-health returns HTML fragment.

        Given I am authenticated
        When I request the health partial
        Then I receive an HTML fragment (not full page)
        """
        response = authenticated_client.get("/admin/partials/dashboard-health")

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        # Should be an HTML fragment, not a full page
        assert (
            "<html>" not in response.text.lower()
        ), "Partial should not contain full HTML structure"
        # Should contain health-related content
        text_lower = response.text.lower()
        assert (
            "health" in text_lower or "status" in text_lower
        ), "Health partial should contain health-related content"

    def test_dashboard_partial_stats(self, authenticated_client: TestClient):
        """
        GET /admin/partials/dashboard-stats returns HTML fragment.

        Given I am authenticated
        When I request the stats partial
        Then I receive an HTML fragment (not full page)
        """
        response = authenticated_client.get("/admin/partials/dashboard-stats")

        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        # Should be an HTML fragment, not a full page
        assert (
            "<html>" not in response.text.lower()
        ), "Partial should not contain full HTML structure"

    def test_partials_require_auth(self, web_client: TestClient):
        """
        Partial endpoints require authentication.

        Given I am not authenticated
        When I request a partial endpoint
        Then I am redirected to login
        """
        # Test health partial
        response = web_client.get("/admin/partials/dashboard-health")
        assert response.status_code in [
            302,
            303,
        ], f"Health partial should redirect unauthenticated, got {response.status_code}"

        # Test stats partial
        response = web_client.get("/admin/partials/dashboard-stats")
        assert response.status_code in [
            302,
            303,
        ], f"Stats partial should redirect unauthenticated, got {response.status_code}"
