"""Unit tests for admin jobs stats API endpoint."""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from datetime import datetime

# Import after mocking modules that may not exist
with patch.dict(
    "sys.modules",
    {"tantivy": MagicMock(), "filesystem_client": MagicMock(), "voyageai": MagicMock()},
):
    from code_indexer.server.app import create_app


class TestAdminJobsStatsEndpoint:
    """Tests for the GET /api/admin/jobs/stats endpoint."""

    @pytest.fixture
    def client(self):
        """Create a test client."""
        app = create_app()
        return TestClient(app)

    @pytest.fixture
    def admin_headers(self):
        """Mock admin authentication headers."""
        return {"Authorization": "Bearer admin-token"}

    @patch("code_indexer.server.auth.dependencies.jwt_manager")
    @patch("code_indexer.server.auth.dependencies.user_manager")
    def test_stats_endpoint_exists(
        self, mock_user_manager, mock_jwt_manager, client, admin_headers
    ):
        """Test that the stats endpoint exists and responds."""
        from code_indexer.server.auth.user_manager import User, UserRole
        from datetime import timezone

        # Setup authentication for admin
        mock_jwt_manager.validate_token.return_value = {
            "username": "admin",
            "role": "admin",
            "exp": 9999999999,
            "iat": 1234567890,
        }

        admin_user = User(
            username="admin",
            password_hash="$2b$12$hash",
            role=UserRole.ADMIN,
            created_at=datetime.now(timezone.utc),
        )
        mock_user_manager.get_user.return_value = admin_user

        # Create client after patches are set
        app = create_app()
        client = TestClient(app)

        # Mock the background_job_manager
        with patch("code_indexer.server.app.background_job_manager") as mock_manager:
            mock_manager.list_jobs.return_value = []

            # Make request
            response = client.get("/api/admin/jobs/stats", headers=admin_headers)

            # Should not return 404
            assert response.status_code != 404, (
                f"Endpoint not found. Response: {response.text}"
            )
