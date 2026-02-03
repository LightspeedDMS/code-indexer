"""
Unit tests for CI/CD GitLab CI REST API endpoints.

Story #745: CI/CD Monitoring REST Endpoints

Tests GitLab CI endpoints with mocked handlers to verify:
- Correct handler invocation
- Response structure
- Parameter passing
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Test constants
# Use numeric project ID for simpler API testing (GitLab supports numeric IDs)
TEST_PROJECT_ID = "12345"
TEST_PIPELINE_ID = 98765432
TEST_JOB_ID = 87654321


class TestGitLabCIEndpoints:
    """Test GitLab CI endpoints with mocked handlers."""

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

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_list_pipelines")
    def test_list_pipelines_calls_handler_with_correct_args(self, mock_handler, client):
        """Test list pipelines endpoint passes correct arguments to handler."""
        mock_handler.return_value = {
            "success": True,
            "pipelines": [],
            "rate_limit": {"limit": 2000, "remaining": 1999, "reset": 1234567890},
        }

        client.get(
            f"/api/cicd/gitlab/{TEST_PROJECT_ID}/pipelines",
            params={"ref": "main", "status": "success", "limit": 5},
        )

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["project_id"] == TEST_PROJECT_ID
        assert call_args["ref"] == "main"
        assert call_args["status"] == "success"
        assert call_args["limit"] == 5

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_list_pipelines")
    def test_list_pipelines_returns_pipelines_array(self, mock_handler, client):
        """Test list pipelines endpoint returns pipelines array."""
        mock_handler.return_value = {
            "success": True,
            "pipelines": [
                {"id": TEST_PIPELINE_ID, "status": "success", "ref": "main"},
            ],
            "rate_limit": {"limit": 2000, "remaining": 1999, "reset": 1234567890},
        }

        response = client.get(f"/api/cicd/gitlab/{TEST_PROJECT_ID}/pipelines")

        assert response.status_code == 200
        data = response.json()
        assert "pipelines" in data
        assert len(data["pipelines"]) == 1

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_get_pipeline")
    def test_get_pipeline_calls_handler_with_pipeline_id(self, mock_handler, client):
        """Test get pipeline endpoint passes pipeline_id to handler."""
        mock_handler.return_value = {
            "success": True,
            "pipeline": {"id": TEST_PIPELINE_ID, "status": "success"},
            "rate_limit": {"limit": 2000, "remaining": 1999, "reset": 1234567890},
        }

        client.get(f"/api/cicd/gitlab/{TEST_PROJECT_ID}/pipelines/{TEST_PIPELINE_ID}")

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["project_id"] == TEST_PROJECT_ID
        assert call_args["pipeline_id"] == TEST_PIPELINE_ID

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_search_logs")
    def test_search_logs_calls_handler_with_pattern(self, mock_handler, client):
        """Test search logs endpoint passes query pattern to handler."""
        mock_handler.return_value = {
            "success": True,
            "matches": [],
            "rate_limit": {"limit": 2000, "remaining": 1999, "reset": 1234567890},
        }

        client.get(
            f"/api/cicd/gitlab/{TEST_PROJECT_ID}/pipelines/{TEST_PIPELINE_ID}/logs",
            params={"query": "error"},
        )

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["pattern"] == "error"

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_get_job_logs")
    def test_get_job_logs_calls_handler_with_job_id(self, mock_handler, client):
        """Test get job logs endpoint passes job_id to handler."""
        mock_handler.return_value = {
            "success": True,
            "logs": "test logs",
            "rate_limit": {"limit": 2000, "remaining": 1999, "reset": 1234567890},
        }

        client.get(f"/api/cicd/gitlab/{TEST_PROJECT_ID}/jobs/{TEST_JOB_ID}/logs")

        mock_handler.assert_called_once()
        call_args = mock_handler.call_args[0][0]
        assert call_args["job_id"] == TEST_JOB_ID

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_retry_pipeline")
    def test_retry_pipeline_calls_handler(self, mock_handler, client):
        """Test retry pipeline endpoint calls handler."""
        mock_handler.return_value = {
            "success": True,
            "pipeline_id": TEST_PIPELINE_ID,
            "message": "Retry triggered",
        }

        response = client.post(
            f"/api/cicd/gitlab/{TEST_PROJECT_ID}/pipelines/{TEST_PIPELINE_ID}/retry"
        )

        assert response.status_code == 200
        mock_handler.assert_called_once()

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_cancel_pipeline")
    def test_cancel_pipeline_calls_handler(self, mock_handler, client):
        """Test cancel pipeline endpoint calls handler."""
        mock_handler.return_value = {
            "success": True,
            "pipeline_id": TEST_PIPELINE_ID,
            "message": "Pipeline cancelled",
        }

        response = client.post(
            f"/api/cicd/gitlab/{TEST_PROJECT_ID}/pipelines/{TEST_PIPELINE_ID}/cancel"
        )

        assert response.status_code == 200
        mock_handler.assert_called_once()


class TestGitLabCIErrorHandling:
    """Test error handling in GitLab CI endpoints."""

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

    @patch("code_indexer.server.routes.cicd.handle_gitlab_ci_list_pipelines")
    def test_handler_error_is_returned(self, mock_handler, client):
        """Test that handler errors are returned in response."""
        mock_handler.return_value = {
            "success": False,
            "error": "GitLab authentication failed",
        }

        response = client.get(f"/api/cicd/gitlab/{TEST_PROJECT_ID}/pipelines")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "error" in data
