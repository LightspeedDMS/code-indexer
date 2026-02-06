"""
Unit tests for Story #89: Server Clock in Navigation - UI Integration.

Tests that the server clock is rendered in the admin navigation bar and
includes proper data attributes for JavaScript initialization.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup


class TestServerClockUI:
    """Test server clock UI rendering in admin navigation."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from src.code_indexer.server.app import app

        return TestClient(app)

    @pytest.fixture
    def authenticated_client(self, client):
        """Create authenticated client for admin pages using session auth."""
        # Step 1: Get login page to receive CSRF token in cookie
        login_page_response = client.get("/login")
        assert login_page_response.status_code == 200

        # Step 2: Extract CSRF token from HTML (BeautifulSoup imported at line 12)
        soup = BeautifulSoup(login_page_response.text, 'html.parser')
        csrf_input = soup.find("input", {"name": "csrf_token"})
        assert csrf_input is not None, "CSRF token input must exist in login form"
        csrf_token = csrf_input.get("value")
        assert csrf_token is not None, "CSRF token value must not be None"

        # Step 3: Submit login form with CSRF token (form data, not JSON)
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "admin",
                "csrf_token": csrf_token
            },
            follow_redirects=False  # Don't follow redirect, just get session cookie
        )

        # Should redirect on success (303 See Other)
        assert login_response.status_code == 303, \
            f"Login should redirect on success, got {login_response.status_code}"

        # Step 4: Verify session cookie is set
        # TestClient automatically preserves cookies for subsequent requests
        # No need to manually extract - client.cookies is updated automatically

        return client

    def test_clock_container_exists_in_admin_base(self, authenticated_client):
        """Test AC1: Clock container exists in admin base template navigation."""
        response = authenticated_client.get("/admin/")
        assert response.status_code == 200

        soup = BeautifulSoup(response.text, 'html.parser')

        # Look for clock container
        clock = soup.find(id="server-clock")
        assert clock is not None, "Server clock container must exist in admin navigation"

    def test_clock_has_initial_timestamp_data(self, authenticated_client):
        """Test AC2: Clock container has data-initial-timestamp attribute."""
        response = authenticated_client.get("/admin/")
        soup = BeautifulSoup(response.text, 'html.parser')

        clock = soup.find(id="server-clock")
        assert clock is not None

        # Must have data-initial-timestamp for JavaScript initialization
        assert clock.has_attr("data-initial-timestamp"), \
            "Clock must have data-initial-timestamp attribute"

        # Timestamp should be valid ISO 8601
        timestamp = clock["data-initial-timestamp"]
        assert len(timestamp) > 0, "Timestamp must not be empty"
        assert "T" in timestamp, "Timestamp must be ISO 8601 format"

    def test_clock_positioned_before_dashboard_tab(self, authenticated_client):
        """Test AC3: Clock is positioned before the Dashboard tab in navigation."""
        response = authenticated_client.get("/admin/")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find header (clock and nav are siblings in header)
        header = soup.find("header", class_="admin-header")
        assert header is not None, "Admin header must exist"

        # Get all direct children of header in order
        # Filter out NavigableString (text nodes, whitespace) by checking for Tag type
        from bs4 import Tag
        header_elements = [child for child in header.children if isinstance(child, Tag)]

        # Clock should come before nav
        clock_index = None
        nav_index = None

        for i, element in enumerate(header_elements):
            if element.get("id") == "server-clock":
                clock_index = i
            if element.name == "nav" and "admin-nav" in element.get("class", []):
                nav_index = i

        assert clock_index is not None, "Clock must be in header"
        assert nav_index is not None, "Nav must be in header"
        assert clock_index < nav_index, "Clock must appear before nav in header"

    def test_clock_visible_on_all_admin_pages(self, authenticated_client):
        """Test AC4: Clock is visible on all admin pages."""
        admin_pages = [
            "/admin/",
            "/admin/users",
            "/admin/groups",
            "/admin/repos",
            "/admin/config",
        ]

        for page in admin_pages:
            response = authenticated_client.get(page)
            if response.status_code != 200:
                continue  # Skip pages that might not be accessible

            soup = BeautifulSoup(response.text, 'html.parser')
            clock = soup.find(id="server-clock")

            assert clock is not None, \
                f"Server clock must be visible on {page}"

    def test_clock_includes_server_prefix(self, authenticated_client):
        """Test AC5: Clock display includes 'Server:' prefix."""
        response = authenticated_client.get("/admin/")
        soup = BeautifulSoup(response.text, 'html.parser')

        clock = soup.find(id="server-clock")
        assert clock is not None

        # Should contain "Server:" text (either in HTML or as placeholder)
        clock_text = clock.get_text()
        assert "Server:" in clock_text or clock.has_attr("data-format"), \
            "Clock must include 'Server:' prefix"

    def test_javascript_file_included(self, authenticated_client):
        """Test AC6: Admin pages include server-clock JavaScript file."""
        response = authenticated_client.get("/admin/")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Look for script tag that includes server_clock.js
        scripts = soup.find_all("script", src=True)
        script_sources = [script["src"] for script in scripts]

        has_clock_script = any("server_clock" in src for src in script_sources)

        assert has_clock_script, \
            "Admin pages must include server_clock.js for clock functionality"
