"""
Unit tests for CI/CD GitHub Actions REST API endpoints.

Story #745: CI/CD Monitoring REST Endpoints

Tests GitHub Actions endpoints with mocked handlers to verify:
- Correct handler invocation
- Response structure
- Parameter passing
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Test constants
TEST_OWNER = "jsbattig"
TEST_REPO = "code-indexer"
TEST_RUN_ID = 12345678
TEST_JOB_ID = 87654321


class TestGitHubActionsEndpoints:
    """Test GitHub Actions endpoints with mocked handlers."""

    @pytest.fixture
    def mock_user(self):
        """Create mock authenticated user."""
        user = MagicMock()
        user.username = "test-user"
        user.permissions = {"repository:read", "repository:write"}
        return user

    @pytest.fixture
    def app_with_mocked_auth(self, mock_user):
        """Create app with mocked authentication."""
        from code_indexer.server.routes.cicd import router as cicd_router
        from code_indexer.server.auth.dependencies import get_current_user

        app = FastAPI()

        async def mock_get_current_user():
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user
        app.include_router(cicd_router)
        return app

    @pytest.fixture
    def client(self, app_with_mocked_auth):
        """Create test client with mocked auth."""
        return TestClient(app_with_mocked_auth)

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_list_runs")
    def test_list_runs_calls_handler_with_correct_args(self, mock_handler, client):
        """Test list runs endpoint passes correct arguments to handler."""
        mock_handler.return_value = {
            "success": True,
            "runs": [],
            "rate_limit": {"limit": 5000, "remaining": 4999, "reset": 1234567890},
        }

        client.get(
            f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/runs",
            params={"branch": "main", "status": "completed", "limit": 5},
        )

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["repository"] == f"{TEST_OWNER}/{TEST_REPO}"
        assert call_args["branch"] == "main"
        assert call_args["status"] == "completed"
        assert call_args["limit"] == 5

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_list_runs")
    def test_list_runs_returns_runs_array(self, mock_handler, client):
        """Test list runs endpoint returns runs array."""
        mock_handler.return_value = {
            "success": True,
            "runs": [
                {"id": TEST_RUN_ID, "name": "CI", "status": "completed"},
            ],
            "rate_limit": {"limit": 5000, "remaining": 4999, "reset": 1234567890},
        }

        response = client.get(f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/runs")

        assert response.status_code == 200
        data = response.json()
        assert "runs" in data
        assert len(data["runs"]) == 1

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_get_run")
    def test_get_run_calls_handler_with_run_id(self, mock_handler, client):
        """Test get run endpoint passes run_id to handler."""
        mock_handler.return_value = {
            "success": True,
            "run": {"id": TEST_RUN_ID, "status": "completed"},
            "rate_limit": {"limit": 5000, "remaining": 4999, "reset": 1234567890},
        }

        client.get(f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/runs/{TEST_RUN_ID}")

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["repository"] == f"{TEST_OWNER}/{TEST_REPO}"
        assert call_args["run_id"] == TEST_RUN_ID

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_search_logs")
    def test_search_logs_calls_handler_with_pattern(self, mock_handler, client):
        """Test search logs endpoint passes query pattern to handler."""
        mock_handler.return_value = {
            "success": True,
            "matches": [],
            "rate_limit": {"limit": 5000, "remaining": 4999, "reset": 1234567890},
        }

        client.get(
            f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/runs/{TEST_RUN_ID}/logs",
            params={"query": "error"},
        )

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["pattern"] == "error"

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_get_job_logs")
    def test_get_job_logs_calls_handler_with_job_id(self, mock_handler, client):
        """Test get job logs endpoint passes job_id to handler."""
        mock_handler.return_value = {
            "success": True,
            "logs": "test logs",
            "rate_limit": {"limit": 5000, "remaining": 4999, "reset": 1234567890},
        }

        client.get(f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/jobs/{TEST_JOB_ID}/logs")

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["job_id"] == TEST_JOB_ID

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_retry_run")
    def test_retry_run_calls_handler(self, mock_handler, client):
        """Test retry run endpoint calls handler."""
        mock_handler.return_value = {
            "success": True,
            "run_id": TEST_RUN_ID,
            "message": "Retry triggered",
        }

        response = client.post(
            f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/runs/{TEST_RUN_ID}/retry"
        )

        assert response.status_code == 200
        mock_handler.assert_called_once()

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_cancel_run")
    def test_cancel_run_calls_handler(self, mock_handler, client):
        """Test cancel run endpoint calls handler."""
        mock_handler.return_value = {
            "success": True,
            "run_id": TEST_RUN_ID,
            "message": "Run cancelled",
        }

        response = client.post(
            f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/runs/{TEST_RUN_ID}/cancel"
        )

        assert response.status_code == 200
        mock_handler.assert_called_once()


class TestGitHubActionsErrorHandling:
    """Test error handling in GitHub Actions endpoints."""

    @pytest.fixture
    def mock_user(self):
        """Create mock authenticated user."""
        user = MagicMock()
        user.username = "test-user"
        return user

    @pytest.fixture
    def app_with_mocked_auth(self, mock_user):
        """Create app with mocked authentication."""
        from code_indexer.server.routes.cicd import router as cicd_router
        from code_indexer.server.auth.dependencies import get_current_user

        app = FastAPI()

        async def mock_get_current_user():
            return mock_user

        app.dependency_overrides[get_current_user] = mock_get_current_user
        app.include_router(cicd_router)
        return app

    @pytest.fixture
    def client(self, app_with_mocked_auth):
        """Create test client."""
        return TestClient(app_with_mocked_auth)

    @patch("code_indexer.server.routes.cicd.handle_gh_actions_list_runs")
    def test_handler_error_is_returned(self, mock_handler, client):
        """Test that handler errors are returned in response."""
        mock_handler.return_value = {
            "success": False,
            "error": "GitHub authentication failed",
        }

        response = client.get(f"/api/cicd/github/{TEST_OWNER}/{TEST_REPO}/runs")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "error" in data
