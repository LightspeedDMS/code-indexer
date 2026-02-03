"""Unit tests for maintenance router timeout endpoint (Bug #135).

Bug #135: Auto-update drain timeout must be dynamically calculated from server config.

Tests for /api/admin/maintenance/drain-timeout endpoint.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock


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

    def test_uses_config_manager_to_get_config(self):
        """Endpoint should use ServerConfigManager to retrieve current config."""
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            ServerConfigManager,
        )
        from code_indexer.server.routers.maintenance_router import get_config_manager
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.auth.dependencies import get_current_admin_user
        from code_indexer.server.app import app
        from datetime import datetime, timezone

        # Create mock config manager
        mock_manager = MagicMock(spec=ServerConfigManager)
        mock_config = ServerConfig(server_dir="/tmp/test")
        mock_manager.load_config.return_value = mock_config

        # Override dependencies
        admin_user = User(
            username="admin",
            password_hash="hashed_password",
            role=UserRole.ADMIN,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )

        app.dependency_overrides[get_current_admin_user] = lambda: admin_user
        app.dependency_overrides[get_config_manager] = lambda: mock_manager

        client = TestClient(app)

        try:
            response = client.get("/api/admin/maintenance/drain-timeout")

            assert response.status_code == 200
            # Verify config manager load_config was called
            mock_manager.load_config.assert_called_once()
        finally:
            app.dependency_overrides.clear()

    def test_handles_missing_config_gracefully(self, test_app_with_auth):
        """Endpoint should handle missing config by using defaults."""
        client, auth_headers = test_app_with_auth

        with patch(
            "code_indexer.server.routers.maintenance_router.get_config_manager"
        ) as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.load_config.return_value = None  # No config file
            mock_manager.create_default_config.return_value = MagicMock(
                server_dir="/tmp/test",
                resource_config=MagicMock(
                    git_refresh_timeout=3600,
                    cidx_index_timeout=3600,
                ),
                scip_config=MagicMock(
                    indexing_timeout_seconds=3600,
                    scip_generation_timeout_seconds=600,
                ),
            )
            mock_get_manager.return_value = mock_manager

            response = client.get(
                "/api/admin/maintenance/drain-timeout", headers=auth_headers
            )

            # Should still succeed with default config
            assert response.status_code == 200
            data = response.json()
            assert data["max_job_timeout_seconds"] == 3600
            assert data["recommended_drain_timeout_seconds"] == 5400  # 3600 * 1.5


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
