"""
Simple unit tests for Job Management API endpoints.

Tests for the new job management API functionality following
the existing test patterns in this project.

NOTE ON @pytest.mark.e2e: pre-existing marker (commit a025c759, "mark 488 E2E
tests, achieve 100% pass rate in server-fast-automation.sh") used purely as a
`-m "not e2e"` exclusion tag for server-fast-automation.sh's chunked run, not a
statement that this file follows zero-mock E2E conventions. Left unchanged --
retagging is out of scope for the mock-target fix below.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
from datetime import datetime, timezone

from code_indexer.server.app import create_app
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.background_jobs import BackgroundJobManager


@pytest.mark.e2e
class TestJobManagementAPI:
    """Test job management API endpoints."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        app = create_app()
        return TestClient(app)

    @pytest.fixture
    def mock_auth(self, client):
        """
        Patch the auth-dependency JWT/user-manager pair together.

        Consolidates what used to be two separate stacked @patch decorators
        (jwt_manager, user_manager) repeated on every test into one fixture,
        so each test only needs one additional @patch.object for the manager
        method under test.

        Depends explicitly on `client` so pytest resolves create_app() (which
        (re)assigns real objects onto
        code_indexer.server.auth.dependencies.jwt_manager / .user_manager)
        BEFORE these patches are entered -- otherwise, if pytest happened to
        set up this fixture first, create_app() would stomp the mocks and
        every request would 401 against a real, un-mocked JWTManager.
        """
        with (
            patch("code_indexer.server.auth.dependencies.jwt_manager") as mock_jwt,
            patch(
                "code_indexer.server.auth.dependencies.user_manager"
            ) as mock_user_mgr,
        ):
            yield mock_jwt, mock_user_mgr

    @patch.object(BackgroundJobManager, "list_jobs")
    def test_list_jobs_endpoint(self, mock_list_jobs, mock_auth, client):
        """Test GET /api/jobs endpoint for listing jobs."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "user",
            "exp": 9999999999,
        }

        test_user = User(
            username="testuser",
            password_hash="$2b$12$test_hash",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )
        mock_dep_user_manager.get_user.return_value = test_user

        # Mock job listing
        mock_list_jobs.return_value = {
            "jobs": [
                {
                    "job_id": "job-1",
                    "operation_type": "test_op",
                    "status": "completed",
                    "created_at": "2023-01-01T00:00:00Z",
                    "started_at": "2023-01-01T00:01:00Z",
                    "completed_at": "2023-01-01T00:02:00Z",
                    "progress": 100,
                    "result": None,
                    "error": None,
                    "username": "testuser",
                }
            ],
            "total": 1,
            "limit": 10,
            "offset": 0,
        }

        # Use authorization header
        headers = {"Authorization": "Bearer fake_token"}
        response = client.get("/api/jobs", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert len(data["jobs"]) == 1
        assert data["total"] == 1

        # Verify that list_jobs was called with correct parameters
        mock_list_jobs.assert_called_once_with(
            username="testuser",
            status_filter=None,
            limit=10,
            offset=0,
            is_admin=False,
        )

    @patch.object(BackgroundJobManager, "get_job_status")
    def test_get_job_status_endpoint(self, mock_get_job_status, mock_auth, client):
        """Test GET /api/jobs/{job_id} endpoint."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "user",
            "exp": 9999999999,
        }

        test_user = User(
            username="testuser",
            password_hash="$2b$12$test_hash",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )
        mock_dep_user_manager.get_user.return_value = test_user

        # Mock job status
        mock_get_job_status.return_value = {
            "job_id": "test-job-123",
            "operation_type": "test_operation",
            "status": "completed",
            "created_at": "2023-01-01T00:00:00Z",
            "started_at": "2023-01-01T00:01:00Z",
            "completed_at": "2023-01-01T00:02:00Z",
            "progress": 100,
            "result": {"status": "success"},
            "error": None,
            "username": "testuser",
        }

        headers = {"Authorization": "Bearer fake_token"}
        response = client.get("/api/jobs/test-job-123", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "test-job-123"
        assert data["username"] == "testuser"

        # Verify that get_job_status was called with username for isolation
        mock_get_job_status.assert_called_once_with(
            "test-job-123", "testuser", is_admin=False
        )

    @patch.object(BackgroundJobManager, "cancel_job")
    def test_cancel_job_endpoint(self, mock_cancel_job, mock_auth, client):
        """Test DELETE /api/jobs/{job_id} for job cancellation."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "user",
            "exp": 9999999999,
        }

        test_user = User(
            username="testuser",
            password_hash="$2b$12$test_hash",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )
        mock_dep_user_manager.get_user.return_value = test_user

        # Mock successful cancellation
        mock_cancel_job.return_value = {
            "success": True,
            "message": "Job cancelled successfully",
        }

        headers = {"Authorization": "Bearer fake_token"}
        response = client.delete("/api/jobs/test-job-123", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "cancelled successfully" in data["message"]

        # Verify cancel_job was called with user isolation
        mock_cancel_job.assert_called_once_with(
            "test-job-123", "testuser", is_admin=False
        )
