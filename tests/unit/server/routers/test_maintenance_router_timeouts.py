"""Unit tests for maintenance router timeout endpoint (Bug #135).

Bug #135: Auto-update drain timeout must be dynamically calculated from server config.

Tests for /api/admin/maintenance/drain-timeout endpoint.
"""

import pytest
from fastapi.testclient import TestClient


class TestDrainTimeoutEndpoint:
    """Test /api/admin/maintenance/drain-timeout endpoint."""

    def test_endpoint_requires_authentication(self):
        """Endpoint should require admin authentication."""
        from code_indexer.server.app import app

        client = TestClient(app)

        # Request without auth token should fail (401 unauthorized)
        response = client.get("/api/admin/maintenance/drain-timeout")
        assert response.status_code == 401

    def test_returns_max_job_timeout_and_recommended_drain_timeout(
        self, test_app_with_auth
    ):
        """Endpoint should return both max_job_timeout and recommended_drain_timeout."""
        client, auth_headers = test_app_with_auth

        response = client.get(
            "/api/admin/maintenance/drain-timeout", headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()

        # Should include both fields
        assert "max_job_timeout_seconds" in data
        assert "recommended_drain_timeout_seconds" in data

        # Both should be positive integers
        assert isinstance(data["max_job_timeout_seconds"], int)
        assert isinstance(data["recommended_drain_timeout_seconds"], int)
        assert data["max_job_timeout_seconds"] > 0
        assert data["recommended_drain_timeout_seconds"] > 0

    def test_recommended_timeout_is_one_and_half_times_max(self, test_app_with_auth):
        """Recommended drain timeout should be 1.5x max job timeout."""
        client, auth_headers = test_app_with_auth

        response = client.get(
            "/api/admin/maintenance/drain-timeout", headers=auth_headers
        )

        assert response.status_code == 200
        data = response.json()

        max_timeout = data["max_job_timeout_seconds"]
        recommended = data["recommended_drain_timeout_seconds"]

        # Recommended should be 1.5x max
        assert recommended == int(max_timeout * 1.5)


@pytest.fixture
def test_app_with_auth():
    """Create test app with mocked admin authentication."""
    from datetime import datetime, timezone
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.auth.dependencies import get_current_admin_user
    from code_indexer.server.app import app

    admin_user = User(
        username="admin",
        password_hash="hashed_password",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_current_admin_user] = lambda: admin_user

    client = TestClient(app)

    # Return client and empty headers (auth is mocked via dependency override)
    yield client, {}

    app.dependency_overrides.clear()
