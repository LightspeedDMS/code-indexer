"""
Unit tests for Activity Journal HTMX endpoint and render helper (Story #329).

Tests:
  _render_journal_html() - converts markdown to HTML journal entries
  GET /admin/partials/depmap-activity-journal - incremental content endpoint

Component 4 of Story #329.

Uses real FastAPI test client following patterns from test_dependency_map_routes.py.
Mocking used only where infrastructure boundary requires it.
"""

import re

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (same pattern as test_dependency_map_routes.py)
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
    """Get admin session cookie via form-based login.

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

    # Step 2: POST /login with form data
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
# _render_journal_html() unit tests (pure function, no HTTP needed)
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderJournalHtml:
    """Unit tests for the _render_journal_html() markdown-to-HTML helper."""

    def _get_render_fn(self):
        """Import helper after it is implemented."""
        from code_indexer.server.web.dependency_map_routes import _render_journal_html

        return _render_journal_html

    def test_empty_content_returns_empty_string(self):
        """Empty input yields empty string output."""
        render = self._get_render_fn()
        assert render("") == ""

    def test_whitespace_only_returns_empty_string(self):
        """Whitespace-only input yields empty string."""
        render = self._get_render_fn()
        assert render("   \n  \n") == ""

    def test_wraps_line_in_journal_entry_div(self):
        """Each non-empty line is wrapped in <div class='journal-entry'>."""
        render = self._get_render_fn()
        result = render("hello world")
        assert '<div class="journal-entry">' in result
        assert "hello world" in result
        assert "</div>" in result

    def test_renders_bold_markdown(self):
        """**text** is converted to <strong>text</strong>."""
        render = self._get_render_fn()
        result = render("**system** message")
        assert "<strong>system</strong>" in result
        assert "**" not in result

    def test_renders_inline_code_markdown(self):
        """`code` is converted to <code>code</code>."""
        render = self._get_render_fn()
        result = render("`my_function()` was called")
        assert "<code>my_function()</code>" in result
        assert "`" not in result

    def test_handles_multiple_lines(self):
        """Multiple non-empty lines each get their own journal-entry div."""
        render = self._get_render_fn()
        result = render("line one\nline two\nline three")
        assert result.count('<div class="journal-entry">') == 3

    def test_skips_blank_lines_in_multiline(self):
        """Blank lines in multi-line input are skipped (no empty divs)."""
        render = self._get_render_fn()
        result = render("line one\n\nline two")
        assert result.count('<div class="journal-entry">') == 2

    def test_combines_bold_and_code_on_same_line(self):
        """A line with both **bold** and `code` renders both conversions."""
        render = self._get_render_fn()
        result = render("[10:23:45] **system** called `run_analysis()`")
        assert "<strong>system</strong>" in result
        assert "<code>run_analysis()</code>" in result

    def test_journal_entry_content_preserved(self):
        """The text content (minus markdown) is preserved inside the div."""
        render = self._get_render_fn()
        result = render("plain text entry")
        assert "plain text entry" in result

    def test_html_entities_are_escaped(self):
        """XSS payloads are neutralized - script tags are HTML-escaped."""
        render = self._get_render_fn()
        result = render("<script>alert(1)</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_img_tag_escaped(self):
        """XSS via img onerror is neutralized - img tags are HTML-escaped."""
        render = self._get_render_fn()
        result = render("[10:00:00] **system** <img src=x onerror=alert(1)>")
        assert "<img" not in result
        assert "&lt;img" in result


# ─────────────────────────────────────────────────────────────────────────────
# GET /admin/partials/depmap-activity-journal endpoint tests
# ─────────────────────────────────────────────────────────────────────────────


class TestDepmapActivityJournalEndpoint:
    """Integration tests for the activity journal HTMX partial endpoint."""

    ENDPOINT = "/admin/partials/depmap-activity-journal"

    def test_endpoint_returns_200_for_admin(self, client, admin_session_cookie):
        """Admin can access the activity journal endpoint."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200

    def test_endpoint_returns_401_for_unauthenticated(self, client):
        """Unauthenticated request returns 401."""
        response = client.get(self.ENDPOINT, follow_redirects=False)
        assert response.status_code == 401

    def test_endpoint_returns_html_content_type(self, client, admin_session_cookie):
        """Response content-type is text/html."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_endpoint_returns_x_journal_offset_header(
        self, client, admin_session_cookie
    ):
        """Response includes X-Journal-Offset header."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert "x-journal-offset" in headers_lower

    def test_endpoint_returns_x_journal_progress_header(
        self, client, admin_session_cookie
    ):
        """Response includes X-Journal-Progress header."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert "x-journal-progress" in headers_lower

    def test_endpoint_returns_x_journal_progress_info_header(
        self, client, admin_session_cookie
    ):
        """Response includes X-Journal-Progress-Info header."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert "x-journal-progress-info" in headers_lower

    def test_endpoint_offset_header_is_numeric(self, client, admin_session_cookie):
        """X-Journal-Offset header value is a valid integer string."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        offset_val = headers_lower.get("x-journal-offset", "")
        assert offset_val.isdigit(), f"Expected numeric offset, got: '{offset_val}'"

    def test_endpoint_progress_header_is_numeric(self, client, admin_session_cookie):
        """X-Journal-Progress header value is a valid integer string."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        progress_val = headers_lower.get("x-journal-progress", "")
        assert (
            progress_val.isdigit()
        ), f"Expected numeric progress, got: '{progress_val}'"

    def test_endpoint_returns_empty_body_when_no_active_journal(
        self, client, admin_session_cookie
    ):
        """When no journal is active, body is empty (no active analysis)."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        # No active journal means empty body (service inactive)
        assert response.text == ""

    def test_endpoint_offset_zero_when_no_active_journal(
        self, client, admin_session_cookie
    ):
        """When no journal is active, X-Journal-Offset is 0."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert headers_lower.get("x-journal-offset") == "0"

    def test_endpoint_accepts_offset_query_param(self, client, admin_session_cookie):
        """Endpoint accepts offset query parameter without error."""
        response = client.get(
            f"{self.ENDPOINT}?offset=42",
            cookies=admin_session_cookie,
        )
        assert response.status_code == 200

    def test_endpoint_handles_missing_service_gracefully(
        self, client, admin_session_cookie
    ):
        """When dep_map_service is None (not available), endpoint returns 200 with empty body."""
        # The service is None in test env (no dep map service configured)
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        # Should still return the required headers
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert "x-journal-offset" in headers_lower
        assert "x-journal-progress" in headers_lower

    def test_endpoint_returns_x_journal_active_header(
        self, client, admin_session_cookie
    ):
        """Response includes X-Journal-Active header."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert "x-journal-active" in headers_lower

    def test_endpoint_x_journal_active_is_zero_when_no_service(
        self, client, admin_session_cookie
    ):
        """When no analysis is running (service None in test env), X-Journal-Active is 0."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        headers_lower = {k.lower(): v for k, v in response.headers.items()}
        assert headers_lower.get("x-journal-active") == "0"


# ─────────────────────────────────────────────────────────────────────────────
# _render_journal_html() content rendering with realistic journal entries
# ─────────────────────────────────────────────────────────────────────────────


class TestRenderJournalHtmlRealistic:
    """Tests with realistic journal entry format from ActivityJournalService."""

    def _get_render_fn(self):
        from code_indexer.server.web.dependency_map_routes import _render_journal_html

        return _render_journal_html

    def test_renders_timestamped_journal_entry(self):
        """Realistic journal entry: '[10:23:45] **system** Analysis started' renders correctly."""
        render = self._get_render_fn()
        result = render("[10:23:45] **system** Analysis started")
        assert '<div class="journal-entry">' in result
        assert "<strong>system</strong>" in result
        assert "Analysis started" in result
        assert "[10:23:45]" in result

    def test_renders_code_in_journal_entry(self):
        """Entry with code: 'Processing `my-repo`' renders code tag."""
        render = self._get_render_fn()
        result = render("Processing `my-repo`")
        assert "<code>my-repo</code>" in result

    def test_multi_entry_journal_content(self):
        """Multi-line journal content renders each entry as separate div."""
        render = self._get_render_fn()
        content = (
            "[10:00:01] **system** Start\n"
            "[10:00:02] **analyzer** Pass 1\n"
            "[10:00:03] **system** Done"
        )
        result = render(content)
        assert result.count('<div class="journal-entry">') == 3
        assert "<strong>system</strong>" in result
        assert "<strong>analyzer</strong>" in result


# ─────────────────────────────────────────────────────────────────────────────
# GET /admin/partials/depmap-activity-panel endpoint tests (Story #329 fix)
# ─────────────────────────────────────────────────────────────────────────────


class TestDepmapActivityPanelEndpoint:
    """Integration tests for the activity panel HTMX partial endpoint.

    This endpoint returns the FULL journal panel HTML when analysis is running,
    or empty content when idle. It is independent from the job-status refresh cycle.
    """

    ENDPOINT = "/admin/partials/depmap-activity-panel"

    def test_endpoint_returns_200_for_admin(self, client, admin_session_cookie):
        """Admin can access the activity panel endpoint."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200

    def test_endpoint_returns_401_for_unauthenticated(self, client):
        """Unauthenticated request returns 401."""
        response = client.get(self.ENDPOINT, follow_redirects=False)
        assert response.status_code == 401

    def test_endpoint_returns_html_content_type(self, client, admin_session_cookie):
        """Response content-type is text/html."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_endpoint_returns_empty_body_when_not_running(
        self, client, admin_session_cookie
    ):
        """When no analysis is running, body is empty (panel hidden)."""
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
        # No active analysis in test env - panel template renders empty ({% if is_running %})
        assert response.text.strip() == ""

    def test_endpoint_handles_missing_service_gracefully(
        self, client, admin_session_cookie
    ):
        """When dep_map_service is None (not available), endpoint returns 200 with empty body."""
        # The service is None in test env (no dep map service configured)
        response = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert response.status_code == 200
