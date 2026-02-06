"""
Unit tests for Bug #148: Research Assistant Polling Timeout Issue.

Tests that polling continues indefinitely until server returns definitive status
(complete or error), not based on arbitrary time-based limits.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
import time
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch
from bs4 import BeautifulSoup

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


class TestResearchAssistantPollingBehavior:
    """Test polling behavior for long-running Claude jobs (Bug #148)."""

    # Simulates 12.5 minutes of polling at 5-second intervals (exceeds reported 10-minute timeout)
    LONG_RUNNING_JOB_POLL_COUNT = 150

    def test_poll_endpoint_returns_polling_true_when_job_running(self, client, mock_service):
        """
        Test that poll endpoint returns polling=True when job status is 'running'.

        This ensures the frontend continues polling as long as backend says job is running.
        """
        job_id = "test-job-123"
        session_id = "test-session-456"

        # Mock poll_job to return running status
        mock_service.poll_job.return_value = {
            "status": "running",
            "session_id": session_id,
        }

        # Mock get_messages to return empty list
        mock_service.get_messages.return_value = []

        # Mock render_markdown (no-op for this test)
        mock_service.render_markdown.side_effect = lambda x: x

        response = client.get(f"/admin/research/poll/{job_id}")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

        # Parse HTML and verify polling trigger exists
        soup = BeautifulSoup(response.text, 'html.parser')
        polling_div = soup.find("div", {"hx-get": f"/admin/research/poll/{job_id}"})

        assert polling_div is not None, "Must return polling trigger when job is running"
        assert "hx-trigger" in str(polling_div), "Must have hx-trigger attribute for continued polling"

        # Bug #148 fix: Verify hx-timeout="0" is present to prevent request timeouts
        assert polling_div.get("hx-timeout") == "0", \
            "Polling div must have hx-timeout='0' to prevent HTTP request timeouts"

    def test_poll_endpoint_returns_no_polling_when_job_complete(self, client, mock_service):
        """
        Test that poll endpoint returns polling=False when job status is 'complete'.

        This ensures polling stops when job completes successfully.
        """
        job_id = "test-job-123"
        session_id = "test-session-456"

        # Mock poll_job to return complete status
        mock_service.poll_job.return_value = {
            "status": "complete",
            "session_id": session_id,
            "response": "Claude response here",
        }

        # Mock get_messages to return messages
        mock_service.get_messages.return_value = [
            {
                "id": 1,
                "session_id": session_id,
                "role": "user",
                "content": "Test question",
                "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "id": 2,
                "session_id": session_id,
                "role": "assistant",
                "content": "Claude response here",
                "created_at": "2024-01-01T00:00:30Z",
            },
        ]

        # Mock render_markdown (no-op for this test)
        mock_service.render_markdown.side_effect = lambda x: x

        response = client.get(f"/admin/research/poll/{job_id}")

        assert response.status_code == 200

        # Parse HTML and verify NO polling trigger exists
        soup = BeautifulSoup(response.text, 'html.parser')
        polling_div = soup.find("div", {"hx-get": f"/admin/research/poll/{job_id}"})

        assert polling_div is None, "Must NOT return polling trigger when job is complete"

    def test_poll_endpoint_returns_no_polling_when_job_error(self, client, mock_service):
        """
        Test that poll endpoint returns polling=False when job status is 'error'.

        This ensures polling stops when job fails.
        """
        job_id = "test-job-123"
        session_id = "test-session-456"

        # Mock poll_job to return error status
        mock_service.poll_job.return_value = {
            "status": "error",
            "session_id": session_id,
            "error": "Claude CLI execution failed",
        }

        # Mock get_messages to return messages
        mock_service.get_messages.return_value = [
            {
                "id": 1,
                "session_id": session_id,
                "role": "user",
                "content": "Test question",
                "created_at": "2024-01-01T00:00:00Z",
            },
        ]

        # Mock render_markdown (no-op for this test)
        mock_service.render_markdown.side_effect = lambda x: x

        response = client.get(f"/admin/research/poll/{job_id}")

        assert response.status_code == 200

        # Parse HTML and verify NO polling trigger exists
        soup = BeautifulSoup(response.text, 'html.parser')
        polling_div = soup.find("div", {"hx-get": f"/admin/research/poll/{job_id}"})

        assert polling_div is None, "Must NOT return polling trigger when job errors"

        # Verify error message is displayed
        error_div = soup.find("div", class_="chat-message error")
        assert error_div is not None, "Must display error message when job fails"

    def test_send_endpoint_returns_polling_trigger_with_job_id(self, client, mock_service):
        """
        Test that /send endpoint returns polling trigger immediately after starting job.

        This ensures polling begins right after user sends a message.
        """
        session_id = "test-session-456"
        job_id = "test-job-789"

        # Mock service methods
        mock_service.get_session.return_value = {
            "id": session_id,
            "name": "Test Session",
            "folder_path": "/test/path",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        mock_service.execute_prompt.return_value = job_id

        mock_service.get_messages.return_value = [
            {
                "id": 1,
                "session_id": session_id,
                "role": "user",
                "content": "Test question",
                "created_at": "2024-01-01T00:00:00Z",
            },
        ]

        mock_service.get_all_sessions.return_value = [
            {
                "id": session_id,
                "name": "Test Session",
                "folder_path": "/test/path",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        ]

        # Mock render_markdown (no-op for this test)
        mock_service.render_markdown.side_effect = lambda x: x

        response = client.post(
            "/admin/research/send",
            data={
                "user_prompt": "Test question",
                "session_id": session_id,
            }
        )

        assert response.status_code == 200

        # Parse HTML and verify polling trigger with correct job_id
        soup = BeautifulSoup(response.text, 'html.parser')
        polling_div = soup.find("div", {"hx-get": f"/admin/research/poll/{job_id}"})

        assert polling_div is not None, "Must return polling trigger after sending message"
        assert "hx-trigger" in str(polling_div), "Must have hx-trigger attribute to start polling"

        # Bug #148 fix: Verify hx-timeout="0" is present
        assert polling_div.get("hx-timeout") == "0", \
            "Polling div must have hx-timeout='0' to prevent HTTP request timeouts"

    def test_polling_template_has_no_client_side_timeout_logic(self, client, mock_service):
        """
        Test that polling templates do NOT include any JavaScript timeout logic.

        This is a regression test for Bug #148 - ensures we don't introduce
        arbitrary timeouts that stop polling before Claude job completes.

        NOTE: hx-timeout="0" is ALLOWED (and required) - it sets infinite timeout.
        """
        job_id = "test-job-123"
        session_id = "test-session-456"

        # Mock poll_job to return running status
        mock_service.poll_job.return_value = {
            "status": "running",
            "session_id": session_id,
        }

        mock_service.get_messages.return_value = []
        mock_service.render_markdown.side_effect = lambda x: x

        response = client.get(f"/admin/research/poll/{job_id}")

        assert response.status_code == 200

        # Verify NO JavaScript-based timeout logic (patterns that would stop polling)
        response_text = response.text.lower()

        # These patterns indicate harmful timeout mechanisms (NOT hx-timeout="0")
        forbidden_patterns = [
            "max_polls",
            "pollcount",
            "removeattribute('hx-get')",
            "removeattribute('hx-trigger')",
            "settimeout",  # JavaScript setTimeout
        ]

        for pattern in forbidden_patterns:
            assert pattern not in response_text, \
                f"Polling template must NOT contain '{pattern}' - this would cause premature timeout"

        # Bug #148 fix: Verify hx-timeout="0" IS present (infinite timeout)
        assert 'hx-timeout="0"' in response.text, \
            "Polling template MUST have hx-timeout='0' to prevent HTTP request timeouts"

    def test_polling_continues_for_long_running_job(self, client, mock_service):
        """
        Test that polling behavior supports long-running jobs (>10 minutes).

        Simulates 150 polls (150 * 5 seconds = 12.5 minutes) to ensure
        the mechanism supports jobs that exceed the reported 10-minute threshold.
        """
        job_id = "test-long-job-123"
        session_id = "test-session-456"

        for poll_num in range(self.LONG_RUNNING_JOB_POLL_COUNT):
            # Mock poll_job to always return running status
            mock_service.poll_job.return_value = {
                "status": "running",
                "session_id": session_id,
            }

            mock_service.get_messages.return_value = []
            mock_service.render_markdown.side_effect = lambda x: x

            response = client.get(f"/admin/research/poll/{job_id}")

            assert response.status_code == 200, \
                f"Poll #{poll_num + 1} failed - polling should continue indefinitely"

            # Verify polling trigger still exists
            soup = BeautifulSoup(response.text, 'html.parser')
            polling_div = soup.find("div", {"hx-get": f"/admin/research/poll/{job_id}"})

            assert polling_div is not None, \
                f"Poll #{poll_num + 1} missing polling trigger - polling stopped prematurely"

            # Bug #148 fix: Verify hx-timeout="0" is present on every poll
            assert polling_div.get("hx-timeout") == "0", \
                f"Poll #{poll_num + 1} missing hx-timeout='0' - HTTP requests may timeout prematurely"

    def test_polling_stops_only_on_completion_after_long_job(self, client, mock_service):
        """
        Test that polling stops ONLY when server returns 'complete' status.

        This verifies the fix for Bug #148 - polling must not stop based on
        time elapsed, only on definitive server response.
        """
        job_id = "test-long-job-456"
        session_id = "test-session-789"

        # Simulate job completion after long run
        mock_service.poll_job.return_value = {
            "status": "complete",
            "session_id": session_id,
            "response": "Final response after long job",
        }

        mock_service.get_messages.return_value = [
            {
                "id": 1,
                "session_id": session_id,
                "role": "assistant",
                "content": "Final response after long job",
                "created_at": "2024-01-01T00:12:30Z",
            },
        ]

        mock_service.render_markdown.side_effect = lambda x: x

        response = client.get(f"/admin/research/poll/{job_id}")
        assert response.status_code == 200

        # Verify polling stops ONLY when status is complete
        soup = BeautifulSoup(response.text, 'html.parser')
        polling_div = soup.find("div", {"hx-get": f"/admin/research/poll/{job_id}"})

        assert polling_div is None, \
            "Polling should stop ONLY when job status is 'complete', not before"
