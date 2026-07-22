"""
Unit tests for Job Management API endpoints.

Tests for the new job management API functionality including
job listing, cancellation, and enhanced status endpoints.

NOTE ON @pytest.mark.e2e: pre-existing marker (commit a025c759, "mark 488 E2E
tests, achieve 100% pass rate in server-fast-automation.sh") used purely as a
`-m "not e2e"` exclusion tag for server-fast-automation.sh's chunked run, not a
statement that this file follows zero-mock E2E conventions. Left unchanged --
retagging is out of scope for the mock-target fix below.
"""

from unittest.mock import Mock, patch
import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import create_app
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


@pytest.fixture
def client():
    """Create test client."""
    app = create_app()
    return TestClient(app)


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    user = Mock()
    user.username = "testuser"
    user.is_admin = False
    return user


@pytest.fixture
def mock_admin_user():
    """Mock authenticated admin user."""
    user = Mock()
    user.username = "admin"
    user.is_admin = True
    return user


@pytest.fixture
def mock_auth(client):
    """
    Patch the auth-dependency JWT/user-manager pair together.

    Consolidates what used to be two separate stacked @patch decorators
    (jwt_manager, user_manager) repeated on every test into one fixture, so
    each test only needs one additional @patch.object for the manager method
    under test.

    Depends explicitly on `client` so pytest resolves create_app() (which
    (re)assigns real objects onto code_indexer.server.auth.dependencies.jwt_manager
    / .user_manager) BEFORE these patches are entered -- otherwise, if pytest
    happened to set up this fixture first, create_app() would stomp the mocks
    and every request would 401 against a real, un-mocked JWTManager.
    """
    with (
        patch("code_indexer.server.auth.dependencies.jwt_manager") as mock_jwt,
        patch("code_indexer.server.auth.dependencies.user_manager") as mock_user_mgr,
    ):
        yield mock_jwt, mock_user_mgr


@pytest.mark.e2e
class TestJobManagementAPI:
    """Test job management API endpoints."""

    @patch.object(BackgroundJobManager, "get_job_status")
    def test_get_job_status_with_user_isolation(
        self,
        mock_get_job_status,
        mock_auth,
        client,
        mock_user,
    ):
        """Test GET /api/jobs/{job_id} with user isolation."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

        # Mock job status for the user
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

        response = client.get(
            "/api/jobs/test-job-123",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "test-job-123"
        assert data["username"] == "testuser"

        # Verify that get_job_status was called with username for isolation
        mock_get_job_status.assert_called_once_with(
            "test-job-123", "testuser", is_admin=False
        )

    @patch.object(BackgroundJobManager, "get_job_status")
    def test_get_job_status_not_found_or_unauthorized(
        self,
        mock_get_job_status,
        mock_auth,
        client,
        mock_user,
    ):
        """Test GET /api/jobs/{job_id} when job not found or not authorized."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

        # Mock job not found
        mock_get_job_status.return_value = None

        response = client.get(
            "/api/jobs/nonexistent-job",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 404
        assert "Job not found" in response.json()["detail"]

    @patch.object(BackgroundJobManager, "list_jobs")
    def test_list_jobs_endpoint(
        self,
        mock_list_jobs,
        mock_auth,
        client,
        mock_user,
    ):
        """Test GET /api/jobs endpoint for listing jobs."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

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
                    "result": {"status": "success"},
                    "error": None,
                    "username": "testuser",
                },
                {
                    "job_id": "job-2",
                    "operation_type": "test_op2",
                    "status": "running",
                    "created_at": "2023-01-01T00:01:00Z",
                    "started_at": "2023-01-01T00:01:30Z",
                    "completed_at": None,
                    "progress": 50,
                    "result": None,
                    "error": None,
                    "username": "testuser",
                },
            ],
            "total": 2,
            "limit": 10,
            "offset": 0,
        }

        response = client.get(
            "/api/jobs", headers={"Authorization": "Bearer test-token"}
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data["jobs"]) == 2
        assert data["total"] == 2
        assert data["limit"] == 10
        assert data["offset"] == 0

        # Verify list_jobs was called with correct parameters
        mock_list_jobs.assert_called_once_with(
            username="testuser",
            status_filter=None,
            limit=10,
            offset=0,
            is_admin=False,
        )

    @patch.object(BackgroundJobManager, "list_jobs")
    def test_list_jobs_with_filters_and_pagination(
        self,
        mock_list_jobs,
        mock_auth,
        client,
        mock_user,
    ):
        """Test GET /api/jobs with status filter and pagination."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

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
                    "result": {"status": "success"},
                    "error": None,
                    "username": "testuser",
                }
            ],
            "total": 5,
            "limit": 1,
            "offset": 2,
        }

        response = client.get(
            "/api/jobs?status=completed&limit=1&offset=2",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert data["limit"] == 1
        assert data["offset"] == 2

        # Verify filter and pagination parameters
        mock_list_jobs.assert_called_once_with(
            username="testuser",
            status_filter="completed",
            limit=1,
            offset=2,
            is_admin=False,
        )

    @patch.object(BackgroundJobManager, "cancel_job")
    def test_cancel_job_endpoint(
        self,
        mock_cancel_job,
        mock_auth,
        client,
        mock_user,
    ):
        """Test DELETE /api/jobs/{job_id} for job cancellation."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

        # Mock successful cancellation
        mock_cancel_job.return_value = {
            "success": True,
            "message": "Job cancelled successfully",
        }

        response = client.delete(
            "/api/jobs/test-job-123",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "cancelled successfully" in data["message"]

        # Verify cancel_job was called with user isolation
        mock_cancel_job.assert_called_once_with(
            "test-job-123", "testuser", is_admin=False
        )

    @patch.object(BackgroundJobManager, "cancel_job")
    def test_cancel_job_unauthorized(
        self,
        mock_cancel_job,
        mock_auth,
        client,
        mock_user,
    ):
        """Test job cancellation when user not authorized."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

        # Mock unauthorized cancellation
        mock_cancel_job.return_value = {
            "success": False,
            "message": "Job not found or not authorized",
        }

        response = client.delete(
            "/api/jobs/unauthorized-job",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 403
        assert "not authorized" in response.json()["detail"]

    @patch.object(BackgroundJobManager, "cancel_job")
    def test_cancel_job_invalid_status(
        self,
        mock_cancel_job,
        mock_auth,
        client,
        mock_user,
    ):
        """Test job cancellation for job in invalid status."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

        # Mock cancellation failure due to status
        mock_cancel_job.return_value = {
            "success": False,
            "message": "Cannot cancel job in completed status",
        }

        response = client.delete(
            "/api/jobs/completed-job",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 400
        assert "Cannot cancel" in response.json()["detail"]

    @patch.object(BackgroundJobManager, "get_job_status")
    def test_enhanced_job_status_response_model(
        self,
        mock_get_job_status,
        mock_auth,
        client,
        mock_user,
    ):
        """Test that job status response includes all new fields."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

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

        response = client.get(
            "/api/jobs/test-job-123",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        data = response.json()

        # Verify all required fields are present
        required_fields = [
            "job_id",
            "operation_type",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "progress",
            "result",
            "error",
            "username",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    @patch.object(BackgroundJobManager, "get_job_status")
    def test_job_status_backward_compatibility(
        self,
        mock_get_job_status,
        mock_auth,
        client,
        mock_user,
    ):
        """Test that existing job status functionality still works."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "testuser",
            "role": "normal_user",
        }
        mock_dep_user_manager.get_user.return_value = mock_user

        # Mock job status in old format (for backward compatibility test)
        mock_get_job_status.return_value = {
            "job_id": "legacy-job",
            "operation_type": "legacy_op",
            "status": "running",
            "created_at": "2023-01-01T00:00:00Z",
            "started_at": "2023-01-01T00:01:00Z",
            "completed_at": None,
            "progress": 50,
            "result": None,
            "error": None,
            "username": "testuser",
        }

        response = client.get(
            "/api/jobs/legacy-job",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "legacy-job"
        assert data["operation_type"] == "legacy_op"
        assert data["status"] == "running"
        assert data["progress"] == 50

    @patch.object(BackgroundJobManager, "cleanup_old_jobs")
    def test_job_cleanup_endpoint(
        self,
        mock_cleanup_old_jobs,
        mock_auth,
        client,
        mock_admin_user,
    ):
        """Test admin endpoint for job cleanup."""
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication
        mock_jwt_manager.validate_token.return_value = {
            "username": "admin",
            "role": "admin",
        }
        mock_dep_user_manager.get_user.return_value = mock_admin_user

        # Mock cleanup operation
        mock_cleanup_old_jobs.return_value = 5

        response = client.delete(
            "/api/admin/jobs/cleanup?max_age_hours=24",
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "cleaned_count" in data
        assert data["cleaned_count"] == 5

        # Verify cleanup was called with correct parameter
        mock_cleanup_old_jobs.assert_called_once_with(max_age_hours=24)

    @patch("code_indexer.server.auth.dependencies.get_current_admin_user")
    def test_job_cleanup_admin_only(
        self, mock_get_current_admin_user, client, mock_user
    ):
        """Test that job cleanup is admin-only."""
        # Setup authentication to fail for non-admin user
        from fastapi import HTTPException, status

        mock_get_current_admin_user.side_effect = HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )

        response = client.delete(
            "/api/admin/jobs/cleanup",
            headers={"Authorization": "Bearer test-token"},
        )

        # Should get 403 or similar auth error for non-admin
        assert response.status_code in [403, 401]

    @patch.object(GoldenRepoManager, "add_golden_repo")
    def test_submit_job_with_username_enhancement(
        self,
        mock_add_golden_repo,
        mock_auth,
        client,
        mock_admin_user,
    ):
        """Test that existing job submission now includes username."""
        # This test verifies that the existing golden repo endpoints
        # work with the enhanced job manager
        mock_jwt_manager, mock_dep_user_manager = mock_auth
        # Setup authentication as admin user
        mock_jwt_manager.validate_token.return_value = {
            "username": "admin",
            "role": "admin",
        }
        mock_dep_user_manager.get_user.return_value = mock_admin_user

        # Mock successful job submission
        mock_add_golden_repo.return_value = "new-job-123"

        # Submit golden repo addition (admin endpoint)
        repo_data = {
            "repo_url": "https://github.com/test/repo.git",
            "alias": "test-repo",
            "default_branch": "main",
        }

        response = client.post(
            "/api/admin/golden-repos",
            json=repo_data,
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 202  # Admin golden repo endpoint returns 202
        data = response.json()
        assert data["job_id"] == "new-job-123"

        # Verify job was submitted with username
        mock_add_golden_repo.assert_called_once()
        call_args = mock_add_golden_repo.call_args
        assert "submitter_username" in call_args.kwargs
        assert call_args.kwargs["submitter_username"] == "admin"
