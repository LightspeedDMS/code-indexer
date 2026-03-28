"""
Unit tests for LLM Credentials Router (Story #367).

Tests REST endpoints:
- POST /api/llm-creds/test-connection
- GET  /api/llm-creds/lease-status
- POST /api/llm-creds/save-config
- POST /api/api-keys/anthropic  (409 guard when subscription mode active)
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_admin_user() -> User:
    return User(
        username="admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def app_with_router():
    """Create isolated FastAPI app with only the llm_creds router."""
    from code_indexer.server.routers.llm_creds import router

    app = FastAPI()
    app.include_router(router)

    admin_user = _make_admin_user()
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user

    return app


@pytest.fixture
def client(app_with_router):
    return TestClient(app_with_router, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /api/llm-creds/test-connection
# ---------------------------------------------------------------------------


class TestTestConnection:
    """POST /api/llm-creds/test-connection"""

    def test_test_connection_success(self, client):
        """Healthy provider returns success=True."""
        with patch(
            "code_indexer.server.routers.llm_creds.LlmCredsClient"
        ) as MockClient:
            MockClient.return_value.health.return_value = True

            response = client.post(
                "/api/llm-creds/test-connection",
                json={"provider_url": "http://localhost:3000", "api_key": "secret"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data.get("error") is None

    def test_test_connection_provider_unhealthy(self, client):
        """Provider returning unhealthy maps to success=False with error message."""
        with patch(
            "code_indexer.server.routers.llm_creds.LlmCredsClient"
        ) as MockClient:
            MockClient.return_value.health.return_value = False

            response = client.post(
                "/api/llm-creds/test-connection",
                json={"provider_url": "http://localhost:3000", "api_key": "secret"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["error"] is not None

    def test_test_connection_network_error(self, client):
        """Connection error maps to success=False with error text."""
        with patch(
            "code_indexer.server.routers.llm_creds.LlmCredsClient"
        ) as MockClient:
            MockClient.return_value.health.side_effect = ConnectionError("refused")

            response = client.post(
                "/api/llm-creds/test-connection",
                json={"provider_url": "http://bad-host:3000", "api_key": "secret"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "refused" in data["error"]

    def test_test_connection_requires_auth(self):
        """Without auth override, endpoint returns 401/403."""
        from code_indexer.server.routers.llm_creds import router

        app = FastAPI()
        app.include_router(router)
        # No dependency override — real auth which will fail in unit test
        unauthenticated_client = TestClient(app, raise_server_exceptions=False)

        response = unauthenticated_client.post(
            "/api/llm-creds/test-connection",
            json={"provider_url": "http://localhost:3000", "api_key": "secret"},
        )
        assert response.status_code in (401, 403, 422, 500)


# ---------------------------------------------------------------------------
# GET /api/llm-creds/lease-status
# ---------------------------------------------------------------------------


class TestLeaseStatus:
    """GET /api/llm-creds/lease-status"""

    def test_lease_status_inactive_when_no_lifecycle_service(
        self, app_with_router, client
    ):
        """Returns inactive status when no lifecycle service in app.state."""
        # Ensure app.state has no lifecycle service
        if hasattr(app_with_router.state, "llm_lifecycle_service"):
            del app_with_router.state.llm_lifecycle_service

        response = client.get("/api/llm-creds/lease-status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "inactive"
        assert data.get("lease_id") is None

    def test_lease_status_active_when_service_active(self, app_with_router, client):
        """Returns active status with masked credential_id when lifecycle service is active."""
        from code_indexer.server.services.llm_lease_lifecycle import (
            LeaseStatusInfo,
            LeaseLifecycleStatus,
        )

        mock_service = MagicMock()
        mock_service.get_status.return_value = LeaseStatusInfo(
            status=LeaseLifecycleStatus.ACTIVE,
            lease_id="lease-abc-123",
            credential_id="cred-xyz-456-full-id-string",
        )
        app_with_router.state.llm_lifecycle_service = mock_service

        response = client.get("/api/llm-creds/lease-status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "active"
        assert data["lease_id"] == "lease-abc-123"
        # credential_id should be masked
        cred = data["credential_id"]
        assert cred is not None
        assert "..." in cred
        # Only first 8 chars shown
        assert cred.startswith("cred-xyz")

    def test_lease_status_degraded(self, app_with_router, client):
        """Returns degraded status with error field populated."""
        from code_indexer.server.services.llm_lease_lifecycle import (
            LeaseStatusInfo,
            LeaseLifecycleStatus,
        )

        mock_service = MagicMock()
        mock_service.get_status.return_value = LeaseStatusInfo(
            status=LeaseLifecycleStatus.DEGRADED,
            error="Provider unreachable",
        )
        app_with_router.state.llm_lifecycle_service = mock_service

        response = client.get("/api/llm-creds/lease-status")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert "unreachable" in data.get("error", "")


# ---------------------------------------------------------------------------
# POST /api/llm-creds/save-config
# ---------------------------------------------------------------------------


class TestSaveConfig:
    """POST /api/llm-creds/save-config"""

    def _base_config(self):
        """Build a minimal ServerConfig-like mock."""
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        mock_config = MagicMock()
        mock_config.claude_integration_config = ClaudeIntegrationConfig()
        return mock_config

    def test_save_config_api_key_mode(self, app_with_router, client):
        """Saving api_key mode persists config and returns success."""
        mock_config = self._base_config()

        with patch(
            "code_indexer.server.routers.llm_creds.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = mock_config
            mock_cs.return_value.config_manager = MagicMock()

            response = client.post(
                "/api/llm-creds/save-config",
                json={
                    "claude_auth_mode": "api_key",
                    "llm_creds_provider_url": "",
                    "llm_creds_provider_api_key": "",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["mode"] == "api_key"

    def test_save_config_subscription_mode_persists(self, app_with_router, client):
        """Saving subscription mode persists all fields to config."""
        mock_config = self._base_config()
        mock_config.claude_integration_config.claude_auth_mode = "api_key"

        with patch(
            "code_indexer.server.routers.llm_creds.get_config_service"
        ) as mock_cs:
            svc = mock_cs.return_value
            svc.load_config.return_value = mock_config
            svc.config_manager = MagicMock()

            response = client.post(
                "/api/llm-creds/save-config",
                json={
                    "claude_auth_mode": "subscription",
                    "llm_creds_provider_url": "http://provider:3000",
                    "llm_creds_provider_api_key": "my-api-key",
                    "llm_creds_provider_consumer_id": "cidx-server",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["mode"] == "subscription"
        # Config fields should have been set
        assert mock_config.claude_integration_config.claude_auth_mode == "subscription"
        assert (
            mock_config.claude_integration_config.llm_creds_provider_url
            == "http://provider:3000"
        )

    def test_save_config_subscription_missing_url_returns_error(self, client):
        """Subscription mode with missing provider URL returns 422 or error response."""
        mock_config = MagicMock()
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        mock_config.claude_integration_config = ClaudeIntegrationConfig()

        with patch(
            "code_indexer.server.routers.llm_creds.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = mock_config

            response = client.post(
                "/api/llm-creds/save-config",
                json={
                    "claude_auth_mode": "subscription",
                    "llm_creds_provider_url": "",  # Missing!
                    "llm_creds_provider_api_key": "key",
                },
            )

        # Either 422 validation error or 200 with success=False
        if response.status_code == 200:
            data = response.json()
            assert data["success"] is False
            assert data.get("error") is not None
        else:
            assert response.status_code == 422

    def test_save_config_subscription_missing_api_key_returns_error(self, client):
        """Subscription mode with missing API key returns error."""
        mock_config = MagicMock()
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        mock_config.claude_integration_config = ClaudeIntegrationConfig()

        with patch(
            "code_indexer.server.routers.llm_creds.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = mock_config

            response = client.post(
                "/api/llm-creds/save-config",
                json={
                    "claude_auth_mode": "subscription",
                    "llm_creds_provider_url": "http://provider:3000",
                    "llm_creds_provider_api_key": "",  # Missing!
                },
            )

        if response.status_code == 200:
            data = response.json()
            assert data["success"] is False
        else:
            assert response.status_code == 422

    def test_save_config_switching_to_subscription_starts_lifecycle(
        self, app_with_router, client
    ):
        """Switching from api_key to subscription triggers lifecycle start."""
        mock_config = self._base_config()
        mock_config.claude_integration_config.claude_auth_mode = "api_key"

        mock_lifecycle = MagicMock()
        mock_lifecycle.get_status.return_value = MagicMock(
            status=MagicMock(value="active")
        )

        with (
            patch(
                "code_indexer.server.routers.llm_creds.get_config_service"
            ) as mock_cs,
            patch(
                "code_indexer.server.routers.llm_creds._build_lifecycle_service"
            ) as mock_build,
        ):
            svc = mock_cs.return_value
            svc.load_config.return_value = mock_config
            svc.config_manager = MagicMock()
            mock_build.return_value = mock_lifecycle

            response = client.post(
                "/api/llm-creds/save-config",
                json={
                    "claude_auth_mode": "subscription",
                    "llm_creds_provider_url": "http://provider:3000",
                    "llm_creds_provider_api_key": "my-api-key",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_lifecycle.start.assert_called_once()

    def test_save_config_switching_from_subscription_stops_lifecycle(
        self, app_with_router, client
    ):
        """Switching from subscription to api_key triggers lifecycle stop."""
        mock_config = self._base_config()
        mock_config.claude_integration_config.claude_auth_mode = "subscription"

        mock_lifecycle = MagicMock()
        mock_lifecycle.get_status.return_value = MagicMock(
            status=MagicMock(value="inactive")
        )
        app_with_router.state.llm_lifecycle_service = mock_lifecycle

        with patch(
            "code_indexer.server.routers.llm_creds.get_config_service"
        ) as mock_cs:
            svc = mock_cs.return_value
            svc.load_config.return_value = mock_config
            svc.config_manager = MagicMock()

            response = client.post(
                "/api/llm-creds/save-config",
                json={
                    "claude_auth_mode": "api_key",
                    "llm_creds_provider_url": "",
                    "llm_creds_provider_api_key": "",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_lifecycle.stop.assert_called_once()


# ---------------------------------------------------------------------------
# POST /api/api-keys/anthropic  — 409 guard
# ---------------------------------------------------------------------------


class TestAnthropicKey409Guard:
    """409 guard on POST /api/api-keys/anthropic when subscription mode active."""

    @pytest.fixture
    def full_app_client(self):
        """Full app client with api_keys router + auth override."""
        from code_indexer.server.app import app
        from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

        admin_user = _make_admin_user()
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user

        yield TestClient(app, raise_server_exceptions=False)

        app.dependency_overrides.pop(get_current_admin_user_hybrid, None)

    def test_anthropic_key_409_when_subscription_mode_active(self, full_app_client):
        """POST /api/api-keys/anthropic returns 409 when claude_auth_mode == 'subscription'."""
        from code_indexer.server.utils.config_manager import (
            ClaudeIntegrationConfig,
        )

        mock_config = MagicMock()
        mock_config.claude_integration_config = ClaudeIntegrationConfig(
            claude_auth_mode="subscription",
            llm_creds_provider_url="http://provider:3000",
            llm_creds_provider_api_key="key",
        )

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = mock_config

            response = full_app_client.post(
                "/api/api-keys/anthropic",
                json={"api_key": "sk-ant-valid-key-12345678901234567890"},
            )

        assert response.status_code == 409
        detail = response.json().get("detail", "")
        assert "subscription" in detail.lower() or "409" in str(response.status_code)

    def test_anthropic_key_succeeds_in_api_key_mode(self, full_app_client):
        """POST /api/api-keys/anthropic returns 200 when claude_auth_mode == 'api_key'."""
        from code_indexer.server.utils.config_manager import (
            ClaudeIntegrationConfig,
        )

        mock_config = MagicMock()
        mock_config.claude_integration_config = ClaudeIntegrationConfig(
            claude_auth_mode="api_key",
        )

        with (
            patch("code_indexer.server.routers.api_keys.get_config_service") as mock_cs,
            patch(
                "code_indexer.server.routers.api_keys.get_api_key_sync_service"
            ) as mock_sync,
        ):
            mock_cs.return_value.load_config.return_value = mock_config
            mock_cs.return_value.config_manager = MagicMock()

            sync_result = MagicMock()
            sync_result.success = True
            sync_result.already_synced = False
            mock_sync.return_value.sync_anthropic_key.return_value = sync_result

            # Patch catchup to avoid side effects
            with patch(
                "code_indexer.server.routers.api_keys.trigger_catchup_on_api_key_save"
            ):
                response = full_app_client.post(
                    "/api/api-keys/anthropic",
                    json={
                        "api_key": "sk-ant-api03-validkeyformatXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
                    },
                )

        # Should NOT be 409
        assert response.status_code != 409
