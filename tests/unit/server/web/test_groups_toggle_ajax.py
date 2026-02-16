"""
Unit tests for Story #199: In-Place Toggle for Group-Repo Access Grid.

This file covers:
- AC1: AJAX toggle without full page reload (JSON responses)
- AC2: Optimistic UI support (immediate response)
- AC3: Error handling with proper JSON error responses
- AC4: CSRF protection via X-CSRF-Token header for AJAX
- AC5: Special cases (cidx-meta, admins) remain read-only
- AC6: Backwards compatibility - form POST still works

TDD: These tests are written FIRST, before implementation.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from code_indexer.server.services.group_access_manager import (
    GroupAccessManager,
    CidxMetaCannotBeRevokedError,
)


@pytest.fixture
def temp_db_path():
    """Create a temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    """Create a GroupAccessManager instance."""
    return GroupAccessManager(temp_db_path)


@pytest.fixture
def test_client(group_manager):
    """Create a test client with mocked session and CSRF."""
    from fastapi import FastAPI
    from code_indexer.server.web.routes import web_router

    app = FastAPI()
    app.include_router(web_router, prefix="/admin")

    # Mock the group manager in app state
    app.state.group_manager = group_manager

    # Mock session manager
    mock_session = MagicMock()
    mock_session.username = "admin_user"
    mock_session.role = "admin"

    with patch("code_indexer.server.web.routes._require_admin_session") as mock_auth:
        mock_auth.return_value = mock_session

        with patch("code_indexer.server.web.routes._get_group_manager") as mock_gm:
            mock_gm.return_value = group_manager

            yield TestClient(app)


class TestAjaxGrantRepoAccess:
    """Tests for AC1, AC2, AC4: AJAX grant with JSON response and CSRF via header."""

    def test_ajax_grant_returns_json_success(self, test_client, group_manager):
        """AC1: AJAX request returns JSON success response without full page reload."""
        # Create a test group
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group"
        )

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            response = test_client.post(
                "/admin/groups/repo-access/grant",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert data["success"] is True

    def test_ajax_grant_validates_csrf_from_header(self, test_client, group_manager):
        """AC4: AJAX request validates CSRF token from X-CSRF-Token header."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = "valid-token"

            # Send invalid CSRF token
            response = test_client.post(
                "/admin/groups/repo-access/grant",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": "invalid-token",
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        assert response.status_code == 403
        data = response.json()
        assert data["success"] is False
        assert "csrf" in data["error"].lower() or "token" in data["error"].lower()

    def test_ajax_grant_creates_access_record(self, test_client, group_manager):
        """AC1: AJAX grant actually creates the access record."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            test_client.post(
                "/admin/groups/repo-access/grant",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        repos = group_manager.get_group_repos(test_group.id)
        assert "test-repo" in repos

    def test_ajax_grant_records_audit_log(self, test_client, group_manager):
        """AC1: AJAX grant records audit log entry."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            test_client.post(
                "/admin/groups/repo-access/grant",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        # Verify audit log was created
        logs, total = group_manager.get_audit_logs(limit=1)
        assert len(logs) > 0
        assert logs[0]["action_type"] == "repo_access_grant"
        assert logs[0]["target_id"] == "test-repo"

    def test_ajax_grant_returns_json_error_on_exception(self, test_client, group_manager):
        """AC3: AJAX grant returns JSON error response on server error."""
        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            # Use nonexistent group to trigger error
            response = test_client.post(
                "/admin/groups/repo-access/grant",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": 99999,
                },
            )

        assert response.status_code >= 400
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert data["success"] is False
        assert "error" in data


class TestAjaxRevokeRepoAccess:
    """Tests for AC1, AC2, AC3, AC4: AJAX revoke with JSON response."""

    def test_ajax_revoke_returns_json_success(self, test_client, group_manager):
        """AC1: AJAX revoke returns JSON success response."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )
        group_manager.grant_repo_access("test-repo", test_group.id, "admin")

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            response = test_client.post(
                "/admin/groups/repo-access/revoke",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert data["success"] is True

    def test_ajax_revoke_validates_csrf_from_header(self, test_client, group_manager):
        """AC4: AJAX revoke validates CSRF from header."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )
        group_manager.grant_repo_access("test-repo", test_group.id, "admin")

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = "valid-token"

            response = test_client.post(
                "/admin/groups/repo-access/revoke",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": "invalid-token",
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        assert response.status_code == 403
        data = response.json()
        assert data["success"] is False

    def test_ajax_revoke_removes_access_record(self, test_client, group_manager):
        """AC1: AJAX revoke actually removes the access record."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )
        group_manager.grant_repo_access("test-repo", test_group.id, "admin")

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            test_client.post(
                "/admin/groups/repo-access/revoke",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        repos = group_manager.get_group_repos(test_group.id)
        assert "test-repo" not in repos

    def test_ajax_revoke_records_audit_log(self, test_client, group_manager):
        """AC1: AJAX revoke records audit log entry."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )
        group_manager.grant_repo_access("test-repo", test_group.id, "admin")

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            test_client.post(
                "/admin/groups/repo-access/revoke",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "test-repo",
                    "group_id": test_group.id,
                },
            )

        logs, total = group_manager.get_audit_logs(limit=1)
        assert len(logs) > 0
        assert logs[0]["action_type"] == "repo_access_revoke"


class TestBackwardsCompatibility:
    """Tests for AC6: Form POST endpoints still work (backwards compatibility)."""

    def test_form_post_grant_still_returns_html_redirect(self, test_client, group_manager):
        """AC6: Regular form POST grant returns full HTML page redirect (not JSON)."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            # Form POST (no X-Requested-With header)
            response = test_client.post(
                "/admin/groups/repo-access/grant",
                data={
                    "csrf_token": csrf_token,
                    "repo_name": "test-repo",
                    "group_id": str(test_group.id),
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        # Should return HTML, not JSON
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        # Verify it's a full page response, not JSON
        assert "success" not in response.json() if response.headers["content-type"] == "application/json" else True

    def test_form_post_revoke_still_returns_html_redirect(self, test_client, group_manager):
        """AC6: Regular form POST revoke returns full HTML page redirect (not JSON)."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )
        group_manager.grant_repo_access("test-repo", test_group.id, "admin")

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            response = test_client.post(
                "/admin/groups/repo-access/revoke",
                data={
                    "csrf_token": csrf_token,
                    "repo_name": "test-repo",
                    "group_id": str(test_group.id),
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_form_post_grant_validates_csrf_from_form_body(self, test_client, group_manager):
        """AC6: Form POST validates CSRF from form body (not header)."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = "valid-token"

            # Send form POST with invalid CSRF in body
            response = test_client.post(
                "/admin/groups/repo-access/grant",
                data={
                    "csrf_token": "invalid-token",
                    "repo_name": "test-repo",
                    "group_id": str(test_group.id),
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        # Should return HTML error page, not JSON
        assert "text/html" in response.headers["content-type"]


class TestSpecialCases:
    """Tests for AC5: Special cases remain read-only (cidx-meta, admins group)."""

    def test_cidx_meta_revoke_protection_in_ajax(self, test_client, group_manager):
        """AC5: Attempting to revoke cidx-meta via AJAX returns error."""
        test_group = group_manager.create_group(
            name="developers",
            description="Developer group",
        )

        csrf_token = "test-csrf-token"

        with patch("code_indexer.server.web.routes.get_csrf_token_from_cookie") as mock_csrf:
            mock_csrf.return_value = csrf_token

            response = test_client.post(
                "/admin/groups/repo-access/revoke",
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRF-Token": csrf_token,
                    "Content-Type": "application/json",
                },
                json={
                    "repo_name": "cidx-meta",
                    "group_id": test_group.id,
                },
            )

        assert response.status_code >= 400
        data = response.json()
        assert data["success"] is False
        assert "cidx-meta" in data["error"].lower()


class TestCsrfTokenEmbedding:
    """Tests for CSRF token embedding in HTML (fix for C1: httponly cookie issue)."""

    def test_groups_page_embeds_csrf_token_in_html(self, test_client, group_manager):
        """CSRF token must be embedded in HTML as data attribute (not just httponly cookie)."""
        csrf_token = "test-csrf-token-12345"

        with patch("code_indexer.server.web.routes.generate_csrf_token") as mock_gen:
            mock_gen.return_value = csrf_token

            with patch("code_indexer.server.web.routes._get_golden_repo_manager"):
                response = test_client.get("/admin/groups")

        assert response.status_code == 200
        html_content = response.text

        # Verify CSRF token is embedded in HTML as data attribute
        assert 'id="csrf-data"' in html_content
        assert f'data-csrf-token="{csrf_token}"' in html_content

    def test_embedded_csrf_token_accessible_to_javascript(self, test_client, group_manager):
        """JavaScript can read CSRF token from data attribute (unlike httponly cookies)."""
        csrf_token = "accessible-token-67890"

        with patch("code_indexer.server.web.routes.generate_csrf_token") as mock_gen:
            mock_gen.return_value = csrf_token

            with patch("code_indexer.server.web.routes._get_golden_repo_manager"):
                response = test_client.get("/admin/groups")

        assert response.status_code == 200
        html_content = response.text

        # Verify the data attribute exists and contains the token
        assert 'data-csrf-token=' in html_content
        assert csrf_token in html_content

        # Verify it's in a hidden element (display:none in style attribute)
        # Note: Jinja2 may render with or without spaces in style attribute
        assert 'display:none' in html_content or 'display: none' in html_content
