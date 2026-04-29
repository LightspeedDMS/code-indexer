"""
Route-level coverage tests for Research Assistant file operation endpoints.

Covers two previously untested routes in research_assistant.py router:
  - DELETE /admin/research/sessions/{session_id}/files/{filename}
  - GET    /admin/research/sessions/{session_id}/files/{filename}

These are safety-net tests to protect the routes during inline_routes.py refactoring.
They verify: route registration, auth required, path-traversal rejection,
file-not-found, and success paths.
"""

import time
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from unittest.mock import patch

from code_indexer.server.routers.research_assistant import router
from code_indexer.server.web.auth import require_admin_session, SessionData

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _bypass_elevation(app, router):
    """Override all require_elevation deps so tests can call routes without TOTP setup."""
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in (route.dependencies or []):
            dep_callable = getattr(dep, "dependency", None)
            if dep_callable and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME:
                app.dependency_overrides[dep_callable] = lambda: None


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app with only the research assistant router mounted."""
    test_app = FastAPI()
    test_app.include_router(router)

    async def mock_admin_session():
        return SessionData(
            username="admin",
            role="admin",
            csrf_token="test-csrf-token",
            created_at=time.time(),
        )

    test_app.dependency_overrides[require_admin_session] = mock_admin_session
    _bypass_elevation(test_app, router)
    return test_app


@pytest.fixture
def client(app):
    """TestClient for the minimal app."""
    return TestClient(app)


@pytest.fixture
def mock_service():
    """
    Patch ResearchAssistantService at the router module level.
    Yields the mock *instance* (return_value of the constructor mock).
    """
    with patch(
        "code_indexer.server.routers.research_assistant.ResearchAssistantService"
    ) as MockCls:
        yield MockCls.return_value


# ---------------------------------------------------------------------------
# DELETE /admin/research/sessions/{session_id}/files/{filename}
# ---------------------------------------------------------------------------


class TestDeleteFileRoute:
    """Tests for DELETE /admin/research/sessions/{session_id}/files/{filename}."""

    def test_route_is_registered(self, client, mock_service):
        """Route exists and responds (not 404/405)."""
        mock_service.delete_file.return_value = True

        response = client.delete("/admin/research/sessions/sess-1/files/report.txt")

        assert response.status_code != 404
        assert response.status_code != 405

    def test_auth_required_no_override(self, app):
        """Without auth override, endpoint should reject unauthenticated requests."""
        # Remove the override so the real dependency runs
        app.dependency_overrides.clear()
        unauthenticated_client = TestClient(app, raise_server_exceptions=False)

        response = unauthenticated_client.delete(
            "/admin/research/sessions/sess-1/files/file.txt"
        )

        # Expect a redirect to login (3xx) or an auth error (4xx), never 2xx
        assert response.status_code >= 300

    def test_success_returns_200_with_success_true(self, client, mock_service):
        """Successful delete returns 200 JSON {success: true}."""
        mock_service.delete_file.return_value = True

        response = client.delete("/admin/research/sessions/sess-abc/files/notes.txt")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_file_not_found_returns_404(self, client, mock_service):
        """When service returns False (file absent), route returns 404."""
        mock_service.delete_file.return_value = False

        response = client.delete("/admin/research/sessions/sess-abc/files/missing.txt")

        assert response.status_code == 404
        data = response.json()
        assert data["success"] is False
        assert "error" in data

    def test_service_called_with_correct_args(self, client, mock_service):
        """Service.delete_file is called with session_id and filename from URL."""
        mock_service.delete_file.return_value = True

        client.delete("/admin/research/sessions/my-session/files/data.csv")

        mock_service.delete_file.assert_called_once_with("my-session", "data.csv")

    def test_rejects_filename_with_forward_slash(self, client, mock_service):
        """Filename containing '/' causes 404 — FastAPI interprets %2F as path separator."""
        # URL-encoded slash (%2F) is decoded by FastAPI routing, creating
        # extra path segments that don't match any route -> 404
        response = client.delete(
            "/admin/research/sessions/sess-1/files/..%2Fetc%2Fpasswd"
        )

        assert response.status_code == 404
        # Service must NOT have been called
        mock_service.delete_file.assert_not_called()

    def test_rejects_filename_with_backslash(self, client, mock_service):
        """Filename containing backslash is rejected before reaching service."""
        response = client.delete(
            "/admin/research/sessions/sess-1/files/..%5Cwindows%5Csystem32"
        )

        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False
        mock_service.delete_file.assert_not_called()


# ---------------------------------------------------------------------------
# GET /admin/research/sessions/{session_id}/files/{filename}
# ---------------------------------------------------------------------------


class TestDownloadFileRoute:
    """Tests for GET /admin/research/sessions/{session_id}/files/{filename}."""

    def test_route_is_registered(self, client, mock_service, tmp_path):
        """Route exists and responds (not 404/405)."""
        real_file = tmp_path / "sample.txt"
        real_file.write_bytes(b"hello")
        mock_service.get_file_path.return_value = real_file

        response = client.get("/admin/research/sessions/sess-1/files/sample.txt")

        assert response.status_code != 404
        assert response.status_code != 405

    def test_auth_required_no_override(self, app):
        """Without auth override, endpoint should reject unauthenticated requests."""
        app.dependency_overrides.clear()
        unauthenticated_client = TestClient(app, raise_server_exceptions=False)

        response = unauthenticated_client.get(
            "/admin/research/sessions/sess-1/files/file.txt"
        )

        assert response.status_code >= 300

    def test_success_returns_file_content(self, client, mock_service, tmp_path):
        """Successful download returns 200 with file bytes."""
        content = b"important research data"
        real_file = tmp_path / "research.txt"
        real_file.write_bytes(content)
        mock_service.get_file_path.return_value = real_file

        response = client.get("/admin/research/sessions/sess-abc/files/research.txt")

        assert response.status_code == 200
        assert response.content == content

    def test_file_not_found_returns_404(self, client, mock_service):
        """When service returns None (file absent), route returns 404."""
        mock_service.get_file_path.return_value = None

        response = client.get("/admin/research/sessions/sess-abc/files/ghost.txt")

        assert response.status_code == 404
        data = response.json()
        assert "error" in data

    def test_service_called_with_correct_args(self, client, mock_service, tmp_path):
        """Service.get_file_path is called with session_id and filename from URL."""
        real_file = tmp_path / "doc.pdf"
        real_file.write_bytes(b"%PDF")
        mock_service.get_file_path.return_value = real_file

        client.get("/admin/research/sessions/my-session/files/doc.pdf")

        mock_service.get_file_path.assert_called_once_with("my-session", "doc.pdf")

    def test_rejects_filename_with_forward_slash(self, client, mock_service):
        """Filename containing '/' causes 404 — FastAPI interprets %2F as path separator."""
        response = client.get("/admin/research/sessions/sess-1/files/..%2Fetc%2Fpasswd")

        assert response.status_code == 404
        mock_service.get_file_path.assert_not_called()

    def test_rejects_filename_with_backslash(self, client, mock_service):
        """Filename containing backslash is rejected before reaching service."""
        response = client.get(
            "/admin/research/sessions/sess-1/files/..%5Cwindows%5Csystem32"
        )

        assert response.status_code == 400
        data = response.json()
        assert data["success"] is False
        mock_service.get_file_path.assert_not_called()

    def test_content_disposition_header_set(self, client, mock_service, tmp_path):
        """Download response includes Content-Disposition with filename."""
        real_file = tmp_path / "myreport.txt"
        real_file.write_bytes(b"report content")
        mock_service.get_file_path.return_value = real_file

        response = client.get("/admin/research/sessions/sess-1/files/myreport.txt")

        assert response.status_code == 200
        content_disposition = response.headers.get("content-disposition", "")
        assert "myreport.txt" in content_disposition
