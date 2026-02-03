"""Unit tests for API keys router endpoints.

Story #20: API Key Management for Claude CLI and VoyageAI

Tests cover:
- POST /api/api-keys/anthropic - Save Anthropic API key
- POST /api/api-keys/voyageai - Save VoyageAI API key
- POST /api/api-keys/anthropic/test - Test Anthropic API key connectivity
- POST /api/api-keys/voyageai/test - Test VoyageAI API key connectivity
- GET /api/api-keys/status - Get API key configuration status
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.fixture
def authenticated_client():
    """Create test client with mocked admin authentication."""
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.auth.dependencies import get_current_admin_user
    from code_indexer.server.app import app
    from fastapi.testclient import TestClient

    admin_user = User(
        username="admin",
        password_hash="hashed_password",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    app.dependency_overrides[get_current_admin_user] = lambda: admin_user

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def unauthenticated_client():
    """Create test client without authentication."""
    from code_indexer.server.app import app
    from fastapi.testclient import TestClient

    return TestClient(app)


class TestApiKeysEndpointsRequireAuth:
    """Test that all API key endpoints require admin authentication."""

    def test_save_anthropic_key_requires_auth(self, unauthenticated_client):
        """POST /api/api-keys/anthropic should return 401 without auth."""
        response = unauthenticated_client.post(
            "/api/api-keys/anthropic",
            json={"api_key": "sk-ant-test12345678901234567890123456789012"},
        )
        assert response.status_code == 401

    def test_save_voyageai_key_requires_auth(self, unauthenticated_client):
        """POST /api/api-keys/voyageai should return 401 without auth."""
        response = unauthenticated_client.post(
            "/api/api-keys/voyageai",
            json={"api_key": "pa-testvoyageaikey123"},
        )
        assert response.status_code == 401

    def test_test_anthropic_key_requires_auth(self, unauthenticated_client):
        """POST /api/api-keys/anthropic/test should return 401 without auth."""
        response = unauthenticated_client.post(
            "/api/api-keys/anthropic/test",
            json={"api_key": "sk-ant-test12345678901234567890123456789012"},
        )
        assert response.status_code == 401

    def test_test_voyageai_key_requires_auth(self, unauthenticated_client):
        """POST /api/api-keys/voyageai/test should return 401 without auth."""
        response = unauthenticated_client.post(
            "/api/api-keys/voyageai/test",
            json={"api_key": "pa-testvoyageaikey123"},
        )
        assert response.status_code == 401

    def test_get_status_requires_auth(self, unauthenticated_client):
        """GET /api/api-keys/status should return 401 without auth."""
        response = unauthenticated_client.get("/api/api-keys/status")
        assert response.status_code == 401


class TestSaveAnthropicKeyEndpoint:
    """Test POST /api/api-keys/anthropic endpoint."""

    def test_save_anthropic_key_validates_format(self, authenticated_client):
        """Should return 400 for invalid key format."""
        response = authenticated_client.post(
            "/api/api-keys/anthropic",
            json={"api_key": "invalid-key"},
        )
        assert response.status_code == 400
        assert "Invalid format" in response.json()["detail"]

    def test_save_anthropic_key_rejects_empty(self, authenticated_client):
        """Should return 400 for empty key."""
        response = authenticated_client.post(
            "/api/api-keys/anthropic",
            json={"api_key": ""},
        )
        assert response.status_code == 400

    @patch("code_indexer.server.routers.api_keys.get_api_key_sync_service")
    def test_save_anthropic_key_success(
        self, mock_get_sync_service, authenticated_client
    ):
        """Should return 200 and sync key on valid format."""
        from code_indexer.server.services.api_key_management import SyncResult

        mock_service = MagicMock()
        mock_service.sync_anthropic_key.return_value = SyncResult(success=True)
        mock_get_sync_service.return_value = mock_service

        response = authenticated_client.post(
            "/api/api-keys/anthropic",
            json={"api_key": "sk-ant-api03-validkey123456789012345678901234"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "anthropic"

    @patch("code_indexer.server.routers.api_keys.get_api_key_sync_service")
    def test_save_anthropic_key_already_synced(
        self, mock_get_sync_service, authenticated_client
    ):
        """Should return 200 with already_synced=True for idempotent calls."""
        from code_indexer.server.services.api_key_management import SyncResult

        mock_service = MagicMock()
        mock_service.sync_anthropic_key.return_value = SyncResult(
            success=True, already_synced=True
        )
        mock_get_sync_service.return_value = mock_service

        response = authenticated_client.post(
            "/api/api-keys/anthropic",
            json={"api_key": "sk-ant-api03-validkey123456789012345678901234"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["already_synced"] is True


class TestSaveVoyageAIKeyEndpoint:
    """Test POST /api/api-keys/voyageai endpoint."""

    def test_save_voyageai_key_validates_format(self, authenticated_client):
        """Should return 400 for invalid key format."""
        response = authenticated_client.post(
            "/api/api-keys/voyageai",
            json={"api_key": "invalid-key"},
        )
        assert response.status_code == 400
        assert "Invalid format" in response.json()["detail"]

    def test_save_voyageai_key_rejects_empty(self, authenticated_client):
        """Should return 400 for empty key."""
        response = authenticated_client.post(
            "/api/api-keys/voyageai",
            json={"api_key": ""},
        )
        assert response.status_code == 400

    @patch("code_indexer.server.routers.api_keys.get_api_key_sync_service")
    def test_save_voyageai_key_success(
        self, mock_get_sync_service, authenticated_client
    ):
        """Should return 200 and sync key on valid format."""
        from code_indexer.server.services.api_key_management import SyncResult

        mock_service = MagicMock()
        mock_service.sync_voyageai_key.return_value = SyncResult(success=True)
        mock_get_sync_service.return_value = mock_service

        response = authenticated_client.post(
            "/api/api-keys/voyageai",
            json={"api_key": "pa-validvoyageaikey123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "voyageai"


class TestAnthropicConnectivityEndpoint:
    """Test POST /api/api-keys/anthropic/test endpoint."""

    def test_test_anthropic_key_validates_format(self, authenticated_client):
        """Should return 400 for invalid key format."""
        response = authenticated_client.post(
            "/api/api-keys/anthropic/test",
            json={"api_key": "invalid-key"},
        )
        assert response.status_code == 400

    @patch("code_indexer.server.routers.api_keys.get_api_key_connectivity_tester")
    def test_test_anthropic_key_success(self, mock_get_tester, authenticated_client):
        """Should return connectivity test result."""
        from code_indexer.server.services.api_key_management import (
            ConnectivityTestResult,
        )

        mock_tester = MagicMock()
        mock_tester.test_anthropic_connectivity = AsyncMock(
            return_value=ConnectivityTestResult(
                success=True, provider="anthropic", response_time_ms=150
            )
        )
        mock_get_tester.return_value = mock_tester

        response = authenticated_client.post(
            "/api/api-keys/anthropic/test",
            json={"api_key": "sk-ant-api03-validkey123456789012345678901234"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "anthropic"
        assert data["response_time_ms"] == 150

    @patch("code_indexer.server.routers.api_keys.get_api_key_connectivity_tester")
    def test_test_anthropic_key_failure(self, mock_get_tester, authenticated_client):
        """Should return error details on connectivity failure."""
        from code_indexer.server.services.api_key_management import (
            ConnectivityTestResult,
        )

        mock_tester = MagicMock()
        mock_tester.test_anthropic_connectivity = AsyncMock(
            return_value=ConnectivityTestResult(
                success=False, provider="anthropic", error="Invalid API key"
            )
        )
        mock_get_tester.return_value = mock_tester

        response = authenticated_client.post(
            "/api/api-keys/anthropic/test",
            json={"api_key": "sk-ant-api03-validkey123456789012345678901234"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"] == "Invalid API key"


class TestVoyageAIConnectivityEndpoint:
    """Test POST /api/api-keys/voyageai/test endpoint."""

    def test_test_voyageai_key_validates_format(self, authenticated_client):
        """Should return 400 for invalid key format."""
        response = authenticated_client.post(
            "/api/api-keys/voyageai/test",
            json={"api_key": "invalid-key"},
        )
        assert response.status_code == 400

    @patch("code_indexer.server.routers.api_keys.get_api_key_connectivity_tester")
    def test_test_voyageai_key_success(self, mock_get_tester, authenticated_client):
        """Should return connectivity test result."""
        from code_indexer.server.services.api_key_management import (
            ConnectivityTestResult,
        )

        mock_tester = MagicMock()
        mock_tester.test_voyageai_connectivity = AsyncMock(
            return_value=ConnectivityTestResult(
                success=True, provider="voyageai", response_time_ms=200
            )
        )
        mock_get_tester.return_value = mock_tester

        response = authenticated_client.post(
            "/api/api-keys/voyageai/test",
            json={"api_key": "pa-validvoyageaikey123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "voyageai"
        assert data["response_time_ms"] == 200


class TestApiKeysStatusEndpoint:
    """Test GET /api/api-keys/status endpoint."""

    @patch("code_indexer.server.routers.api_keys.get_config_service")
    def test_get_status_returns_configured_state(
        self, mock_get_config, authenticated_client
    ):
        """Should return configuration status for both providers."""
        mock_config_service = MagicMock()
        mock_config = MagicMock()
        mock_config.claude_integration_config.anthropic_api_key = (
            "sk-ant-api03-configured12345678901234567890"
        )
        mock_config.claude_integration_config.voyageai_api_key = None
        mock_config_service.load_config.return_value = mock_config
        mock_get_config.return_value = mock_config_service

        response = authenticated_client.get("/api/api-keys/status")
        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_configured"] is True
        assert data["voyageai_configured"] is False

    @patch("code_indexer.server.routers.api_keys.get_config_service")
    def test_get_status_both_unconfigured(self, mock_get_config, authenticated_client):
        """Should return false for both when no keys configured."""
        mock_config_service = MagicMock()
        mock_config = MagicMock()
        mock_config.claude_integration_config.anthropic_api_key = None
        mock_config.claude_integration_config.voyageai_api_key = None
        mock_config_service.load_config.return_value = mock_config
        mock_get_config.return_value = mock_config_service

        response = authenticated_client.get("/api/api-keys/status")
        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_configured"] is False
        assert data["voyageai_configured"] is False
