"""
Unit tests for Research Assistant Router - Session Management.

Story #143: Tests for session CRUD routes
- POST /admin/research/sessions (create)
- PUT /admin/research/sessions/{id} (rename)
- DELETE /admin/research/sessions/{id} (delete)
- GET /admin/research/sessions/{id} (load conversation)
"""

import pytest
import time
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch
from code_indexer.server.routers.research_assistant import router
from code_indexer.server.web.auth import require_admin_session, SessionData


@pytest.fixture
def app():
    """Create test FastAPI app with research assistant router."""
    app = FastAPI()
    app.include_router(router)

    # Override authentication dependency
    async def mock_admin_session():
        return SessionData(
            username="admin",
            role="admin",
            csrf_token="test-csrf-token",
            created_at=time.time(),
        )

    app.dependency_overrides[require_admin_session] = mock_admin_session

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def mock_service():
    """Create mock ResearchAssistantService."""
    with patch('code_indexer.server.routers.research_assistant.ResearchAssistantService') as mock:
        yield mock.return_value


class TestSessionCRUDRoutes:
    """Test session CRUD routes (Story #143)."""

    def test_create_session_endpoint(self, client, mock_service):
        """Test POST /admin/research/sessions creates new session."""
        # Mock service response
        mock_service.create_session.return_value = {
            "id": "test-uuid-123",
            "name": "New Session",
            "folder_path": "/home/user/.cidx-server/research/test-uuid-123",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }
        mock_service.get_all_sessions.return_value = [
            mock_service.create_session.return_value
        ]

        response = client.post("/admin/research/sessions")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        mock_service.create_session.assert_called_once()
        mock_service.get_all_sessions.assert_called_once()

    def test_rename_session_endpoint_success(self, client, mock_service):
        """Test PUT /admin/research/sessions/{id} renames session."""
        session_id = "test-uuid-123"
        new_name = "My Investigation"

        mock_service.rename_session.return_value = True
        mock_service.get_all_sessions.return_value = [
            {
                "id": session_id,
                "name": new_name,
                "folder_path": "/path",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:01Z",
            }
        ]

        response = client.put(
            f"/admin/research/sessions/{session_id}",
            data={"new_name": new_name}
        )

        assert response.status_code == 200
        mock_service.rename_session.assert_called_once_with(session_id, new_name)
        mock_service.get_all_sessions.assert_called_once()

    def test_rename_session_endpoint_validation_error(self, client, mock_service):
        """Test PUT /admin/research/sessions/{id} returns error for invalid name."""
        session_id = "test-uuid-123"
        invalid_name = "@invalid!"

        mock_service.rename_session.return_value = False

        response = client.put(
            f"/admin/research/sessions/{session_id}",
            data={"new_name": invalid_name}
        )

        assert response.status_code == 400
        assert "text/html" in response.headers["content-type"]
        mock_service.rename_session.assert_called_once_with(session_id, invalid_name)

    def test_delete_session_endpoint_success(self, client, mock_service):
        """Test DELETE /admin/research/sessions/{id} deletes session."""
        session_id = "test-uuid-123"

        mock_service.delete_session.return_value = True
        mock_service.get_all_sessions.return_value = [
            {
                "id": "other-uuid",
                "name": "Other Session",
                "folder_path": "/path",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        ]

        response = client.delete(f"/admin/research/sessions/{session_id}")

        assert response.status_code == 200
        mock_service.delete_session.assert_called_once_with(session_id)
        mock_service.get_all_sessions.assert_called_once()

    def test_delete_session_endpoint_not_found(self, client, mock_service):
        """Test DELETE /admin/research/sessions/{id} returns error for non-existent session."""
        session_id = "nonexistent-uuid"

        mock_service.delete_session.return_value = False

        response = client.delete(f"/admin/research/sessions/{session_id}")

        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        mock_service.delete_session.assert_called_once_with(session_id)

    def test_delete_last_session_returns_empty_state(self, client, mock_service):
        """Test DELETE returns empty state when no sessions remain."""
        session_id = "test-uuid-123"

        mock_service.delete_session.return_value = True
        mock_service.get_all_sessions.return_value = []  # No sessions left

        response = client.delete(f"/admin/research/sessions/{session_id}")

        assert response.status_code == 200
        # Should render empty state partial
        html = response.text
        assert "empty" in html.lower() or "start" in html.lower()

    def test_load_session_endpoint_success(self, client, mock_service):
        """Test GET /admin/research/sessions/{id} loads session conversation."""
        session_id = "test-uuid-123"

        mock_service.get_session.return_value = {
            "id": session_id,
            "name": "Test Session",
            "folder_path": "/path",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }
        mock_service.get_messages.return_value = [
            {
                "id": 1,
                "session_id": session_id,
                "role": "user",
                "content": "Test question",
                "created_at": "2024-01-01T00:00:00Z",
            }
        ]

        response = client.get(f"/admin/research/sessions/{session_id}")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        mock_service.get_session.assert_called_once_with(session_id)
        mock_service.get_messages.assert_called_once_with(session_id)

    def test_load_session_endpoint_not_found(self, client, mock_service):
        """Test GET /admin/research/sessions/{id} returns error for non-existent session."""
        session_id = "nonexistent-uuid"

        mock_service.get_session.return_value = None

        response = client.get(f"/admin/research/sessions/{session_id}")

        assert response.status_code == 404
        assert "text/html" in response.headers["content-type"]
        mock_service.get_session.assert_called_once_with(session_id)
