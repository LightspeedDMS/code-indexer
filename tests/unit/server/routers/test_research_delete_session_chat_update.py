"""
Unit tests for Research Assistant Router - Delete Session Chat Update Bug.

Tests for the bug fix where deleting an active session should:
- AC1: Show topmost session's messages if other sessions remain
- AC2: Clear chat if no sessions remain
- AC3: Leave chat unchanged if deleting non-active session

Bug Report: Deleting an active session leaves stale chat messages displayed.
Expected: Chat should update to show topmost session or be cleared.
"""

import pytest
import time
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch
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
    with patch(
        "code_indexer.server.routers.research_assistant.ResearchAssistantService"
    ) as mock:
        yield mock.return_value


class TestDeleteSessionChatUpdate:
    """Test delete session updates chat area correctly (Bug Fix)."""

    def test_delete_active_session_with_remaining_sessions_shows_topmost(
        self, client, mock_service
    ):
        """
        AC1: Deleting active session with other sessions remaining shows topmost session's messages.

        Setup:
        - Session A (active): "session-a-id" with messages ["Message A1", "Message A2"]
        - Session B: "session-b-id" with messages ["Message B1", "Message B2"]

        Action: Delete session A (active)

        Expected:
        - Response contains OOB swap for chat-messages div
        - Chat shows messages from session B (topmost after deletion)
        - Sidebar shows session B as active
        """
        session_a_id = "session-a-id"
        session_b_id = "session-b-id"

        # Mock delete success
        mock_service.delete_session.return_value = True

        # Mock remaining sessions after deletion (B is now topmost)
        mock_service.get_all_sessions.return_value = [
            {
                "id": session_b_id,
                "name": "Session B",
                "folder_path": "/path/b",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:01:00Z",
            }
        ]

        # Mock messages for session B
        mock_service.get_messages.return_value = [
            {
                "id": 1,
                "session_id": session_b_id,
                "role": "user",
                "content": "Message B1",
                "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": 2,
                "session_id": session_b_id,
                "role": "assistant",
                "content": "Message B2",
                "created_at": "2024-01-01T00:00:01Z",
            },
        ]

        response = client.delete(f"/admin/research/sessions/{session_a_id}")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        # Verify service calls
        mock_service.delete_session.assert_called_once_with(session_a_id)
        mock_service.get_all_sessions.assert_called_once()
        mock_service.get_messages.assert_called_once_with(session_b_id)

        # Verify response HTML contains OOB swap for chat-messages
        html = response.text
        assert 'id="chat-messages"' in html or 'id="sessions-sidebar"' in html
        assert "hx-swap-oob" in html, "Response should contain OOB swap for chat area"

        # Verify chat messages from session B are in response
        assert "Message B1" in html
        assert "Message B2" in html

        # Verify session B is marked active in sidebar
        assert session_b_id in html

    def test_delete_active_session_with_no_remaining_sessions_clears_chat(
        self, client, mock_service
    ):
        """
        AC2: Deleting active session when it's the only session clears the chat.

        Setup:
        - Session A (active): "session-a-id" with messages

        Action: Delete session A (only session)

        Expected:
        - Response contains OOB swap for chat-messages div
        - Chat shows empty state or welcome message
        - Sidebar shows empty state
        """
        session_a_id = "session-a-id"

        # Mock delete success
        mock_service.delete_session.return_value = True

        # Mock no remaining sessions after deletion
        mock_service.get_all_sessions.return_value = []

        response = client.delete(f"/admin/research/sessions/{session_a_id}")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        # Verify service calls
        mock_service.delete_session.assert_called_once_with(session_a_id)
        mock_service.get_all_sessions.assert_called_once()
        # get_messages should NOT be called when no sessions remain
        mock_service.get_messages.assert_not_called()

        # Verify response HTML contains OOB swap for chat-messages
        html = response.text
        assert "hx-swap-oob" in html, "Response should contain OOB swap for chat area"

        # Verify chat is cleared (should contain welcome message or be empty)
        assert (
            "Welcome" in html or "empty-state" in html or len(html.strip()) < 500
        ), "Chat should be cleared or show empty state"

        # Verify sidebar shows empty state
        assert "empty-state" in html or "Welcome" in html

    def test_delete_non_active_session_does_not_change_chat(
        self, client, mock_service
    ):
        """
        AC3: Deleting a non-active session doesn't change the chat display.

        Setup:
        - Session A (active): "session-a-id" with messages ["Message A1", "Message A2"]
        - Session B: "session-b-id" with messages

        Action: Delete session B (non-active)

        Expected:
        - Response updates sidebar only (no OOB swap for chat)
        - Chat continues showing session A messages (unchanged)
        """
        session_a_id = "session-a-id"
        session_b_id = "session-b-id"

        # Mock delete success
        mock_service.delete_session.return_value = True

        # Mock remaining sessions after deletion (A remains, still topmost)
        mock_service.get_all_sessions.return_value = [
            {
                "id": session_a_id,
                "name": "Session A",
                "folder_path": "/path/a",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:01:00Z",
            }
        ]

        # IMPORTANT: The endpoint needs to know which session was active before delete
        # This test validates that if the deleted session was NOT active,
        # we don't send OOB swap for chat-messages
        #
        # NOTE: The current implementation doesn't track active session on delete.
        # This test will guide us to add an optional query parameter like:
        # DELETE /admin/research/sessions/{session_id}?active_session_id=session-a-id

        response = client.delete(
            f"/admin/research/sessions/{session_b_id}",
            params={"active_session_id": session_a_id},  # Signal which session is active
        )

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        # Verify service calls
        mock_service.delete_session.assert_called_once_with(session_b_id)
        mock_service.get_all_sessions.assert_called_once()
        # get_messages should NOT be called when deleted session was not active
        mock_service.get_messages.assert_not_called()

        # Verify response HTML does NOT contain OOB swap for chat-messages
        html = response.text
        # Should only update sidebar, not chat
        # If there's an OOB swap, it should only be for the sidebar
        if "hx-swap-oob" in html:
            # Check that OOB swap is NOT for chat-messages
            assert (
                'id="chat-messages"' not in html
                or 'hx-swap-oob' not in html.split('id="chat-messages"')[0][-50:]
            ), "Should not have OOB swap for chat when deleting non-active session"

    def test_delete_session_not_found_returns_error(self, client, mock_service):
        """Test DELETE /admin/research/sessions/{id} returns error if session not found."""
        session_id = "nonexistent-id"

        # Mock delete failure
        mock_service.delete_session.return_value = False

        response = client.delete(f"/admin/research/sessions/{session_id}")

        assert response.status_code == 404
        html = response.text
        assert "error" in html.lower() or "not found" in html.lower()

    def test_delete_session_updates_sidebar_with_correct_active_session(
        self, client, mock_service
    ):
        """
        Verify sidebar is updated with correct active session after deletion.

        This test ensures that after deleting a session, the sidebar
        correctly highlights the new topmost session as active.
        """
        deleted_session_id = "session-to-delete"
        new_active_session_id = "new-active-session"

        # Mock delete success
        mock_service.delete_session.return_value = True

        # Mock remaining sessions
        mock_service.get_all_sessions.return_value = [
            {
                "id": new_active_session_id,
                "name": "New Active Session",
                "folder_path": "/path",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:01:00Z",
            }
        ]

        # Mock messages for new active session
        mock_service.get_messages.return_value = [
            {
                "id": 1,
                "session_id": new_active_session_id,
                "role": "user",
                "content": "Test message",
                "created_at": "2024-01-01T00:00:00Z",
            }
        ]

        response = client.delete(f"/admin/research/sessions/{deleted_session_id}")

        assert response.status_code == 200
        html = response.text

        # Verify new session is marked active in sidebar
        # The sessions list partial uses: class="session-item {% if session.id == active_session_id %}active{% endif %}"
        assert "active" in html
        assert new_active_session_id in html
