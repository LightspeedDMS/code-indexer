"""
Route-level coverage tests for api_keys.py router — 7 previously uncovered routes.

Safety-net tests for refactoring: verify each route is registered, accepts the
correct HTTP method, and returns the expected response shape/status code.

Routes under test (prefix /api/api-keys):
  1. POST   /voyageai                — Save VoyageAI API key
  2. POST   /anthropic/test          — Test Anthropic key (provided in body)
  3. POST   /voyageai/test           — Test VoyageAI key (provided in body)
  4. POST   /anthropic/test-configured — Test configured Anthropic key
  5. POST   /voyageai/test-configured  — Test configured VoyageAI key
  6. GET    /status                  — Get API key configuration status
  7. DELETE /voyageai                — Delete VoyageAI key

Mocking strategy: only external HTTP calls (VoyageAI/Anthropic) and
config-service I/O are mocked.  Auth dependency is overridden via
app.dependency_overrides so we test with a real admin user object.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Valid key fixtures — satisfy ApiKeyValidator format rules:
#   Anthropic: starts with "sk-ant-", min 40 chars
#   VoyageAI:  starts with "pa-",     min 20 chars
VALID_ANTHROPIC_KEY = "sk-ant-api03-" + "X" * 30  # 43 chars total
VALID_VOYAGEAI_KEY = "pa-" + "Y" * 20  # 23 chars total

INVALID_ANTHROPIC_KEY = "bad-key"
INVALID_VOYAGEAI_KEY = "bad-key"


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _make_config(anthropic_key: str = "", voyageai_key: str = "") -> MagicMock:
    """Build a minimal config mock with ClaudeIntegrationConfig fields."""
    from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

    cfg = MagicMock()
    cfg.claude_integration_config = ClaudeIntegrationConfig(
        claude_auth_mode="api_key",
        anthropic_api_key=anthropic_key,
        voyageai_api_key=voyageai_key,
    )
    return cfg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_router():
    """Isolated FastAPI app that includes only the api_keys router."""
    from code_indexer.server.routers.api_keys import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_admin_user_hybrid] = _admin_user
    return app


@pytest.fixture
def client(app_with_router):
    return TestClient(app_with_router, raise_server_exceptions=False)


@pytest.fixture
def unauthenticated_client():
    """Client with no auth override — real auth dependency will reject requests."""
    from code_indexer.server.routers.api_keys import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. POST /api/api-keys/voyageai — Save VoyageAI API key
# ---------------------------------------------------------------------------


class TestSaveVoyageaiKey:
    """POST /api/api-keys/voyageai"""

    def test_save_voyageai_key_success(self, client):
        """Valid key is saved; returns success=True with provider='voyageai'."""
        cfg = _make_config()

        with (
            patch("code_indexer.server.routers.api_keys.get_config_service") as mock_cs,
            patch(
                "code_indexer.server.routers.api_keys.get_api_key_sync_service"
            ) as mock_sync,
        ):
            mock_cs.return_value.load_config.return_value = cfg
            mock_cs.return_value.config_manager = MagicMock()

            sync_result = MagicMock()
            sync_result.success = True
            sync_result.already_synced = False
            mock_sync.return_value.sync_voyageai_key.return_value = sync_result

            response = client.post(
                "/api/api-keys/voyageai",
                json={"api_key": VALID_VOYAGEAI_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "voyageai"
        assert data["already_synced"] is False

    def test_save_voyageai_key_already_synced(self, client):
        """Key already synced returns success=True with already_synced=True."""
        cfg = _make_config()

        with (
            patch("code_indexer.server.routers.api_keys.get_config_service") as mock_cs,
            patch(
                "code_indexer.server.routers.api_keys.get_api_key_sync_service"
            ) as mock_sync,
        ):
            mock_cs.return_value.load_config.return_value = cfg
            mock_cs.return_value.config_manager = MagicMock()

            sync_result = MagicMock()
            sync_result.success = True
            sync_result.already_synced = True
            mock_sync.return_value.sync_voyageai_key.return_value = sync_result

            response = client.post(
                "/api/api-keys/voyageai",
                json={"api_key": VALID_VOYAGEAI_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["already_synced"] is True

    def test_save_voyageai_key_invalid_format(self, client):
        """Malformed key returns 400 with validation error detail."""
        response = client.post(
            "/api/api-keys/voyageai",
            json={"api_key": INVALID_VOYAGEAI_KEY},
        )
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_save_voyageai_key_sync_failure_returns_500(self, client):
        """Sync service failure raises 500."""
        cfg = _make_config()

        with (
            patch("code_indexer.server.routers.api_keys.get_config_service") as mock_cs,
            patch(
                "code_indexer.server.routers.api_keys.get_api_key_sync_service"
            ) as mock_sync,
        ):
            mock_cs.return_value.load_config.return_value = cfg

            sync_result = MagicMock()
            sync_result.success = False
            sync_result.error = "Disk write failure"
            mock_sync.return_value.sync_voyageai_key.return_value = sync_result

            response = client.post(
                "/api/api-keys/voyageai",
                json={"api_key": VALID_VOYAGEAI_KEY},
            )

        assert response.status_code == 500

    def test_save_voyageai_key_requires_auth(self, unauthenticated_client):
        """Without auth the endpoint rejects the request."""
        response = unauthenticated_client.post(
            "/api/api-keys/voyageai",
            json={"api_key": VALID_VOYAGEAI_KEY},
        )
        assert response.status_code in (401, 403, 422, 500)


# ---------------------------------------------------------------------------
# 2. POST /api/api-keys/anthropic/test — Test Anthropic key (body)
# ---------------------------------------------------------------------------


class TestTestAnthropicKey:
    """POST /api/api-keys/anthropic/test"""

    def test_test_anthropic_key_success(self, client):
        """Valid key passes format check and connectivity test returns success."""
        connectivity_result = MagicMock()
        connectivity_result.success = True
        connectivity_result.provider = "anthropic"
        connectivity_result.error = None
        connectivity_result.response_time_ms = 123

        with patch(
            "code_indexer.server.routers.api_keys.get_api_key_connectivity_tester"
        ) as mock_tester:
            mock_tester.return_value.test_anthropic_connectivity = AsyncMock(
                return_value=connectivity_result
            )

            response = client.post(
                "/api/api-keys/anthropic/test",
                json={"api_key": VALID_ANTHROPIC_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "anthropic"
        assert data["response_time_ms"] == 123

    def test_test_anthropic_key_connectivity_failure(self, client):
        """Connectivity failure returns success=False with error message."""
        connectivity_result = MagicMock()
        connectivity_result.success = False
        connectivity_result.provider = "anthropic"
        connectivity_result.error = "Connection refused"
        connectivity_result.response_time_ms = None

        with patch(
            "code_indexer.server.routers.api_keys.get_api_key_connectivity_tester"
        ) as mock_tester:
            mock_tester.return_value.test_anthropic_connectivity = AsyncMock(
                return_value=connectivity_result
            )

            response = client.post(
                "/api/api-keys/anthropic/test",
                json={"api_key": VALID_ANTHROPIC_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "refused" in data["error"]

    def test_test_anthropic_key_invalid_format(self, client):
        """Invalid key format rejected with 400 before connectivity test."""
        response = client.post(
            "/api/api-keys/anthropic/test",
            json={"api_key": INVALID_ANTHROPIC_KEY},
        )
        assert response.status_code == 400

    def test_test_anthropic_key_requires_auth(self, unauthenticated_client):
        """Without auth the endpoint rejects the request."""
        response = unauthenticated_client.post(
            "/api/api-keys/anthropic/test",
            json={"api_key": VALID_ANTHROPIC_KEY},
        )
        assert response.status_code in (401, 403, 422, 500)


# ---------------------------------------------------------------------------
# 3. POST /api/api-keys/voyageai/test — Test VoyageAI key (body)
# ---------------------------------------------------------------------------


class TestTestVoyageaiKey:
    """POST /api/api-keys/voyageai/test"""

    def test_test_voyageai_key_success(self, client):
        """Valid key passes format check and connectivity test returns success."""
        connectivity_result = MagicMock()
        connectivity_result.success = True
        connectivity_result.provider = "voyageai"
        connectivity_result.error = None
        connectivity_result.response_time_ms = 99

        with patch(
            "code_indexer.server.routers.api_keys.get_api_key_connectivity_tester"
        ) as mock_tester:
            mock_tester.return_value.test_voyageai_connectivity = AsyncMock(
                return_value=connectivity_result
            )

            response = client.post(
                "/api/api-keys/voyageai/test",
                json={"api_key": VALID_VOYAGEAI_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "voyageai"
        assert data["response_time_ms"] == 99

    def test_test_voyageai_key_connectivity_failure(self, client):
        """Connectivity failure returns success=False with error message."""
        connectivity_result = MagicMock()
        connectivity_result.success = False
        connectivity_result.provider = "voyageai"
        connectivity_result.error = "Unauthorized"
        connectivity_result.response_time_ms = None

        with patch(
            "code_indexer.server.routers.api_keys.get_api_key_connectivity_tester"
        ) as mock_tester:
            mock_tester.return_value.test_voyageai_connectivity = AsyncMock(
                return_value=connectivity_result
            )

            response = client.post(
                "/api/api-keys/voyageai/test",
                json={"api_key": VALID_VOYAGEAI_KEY},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Unauthorized" in data["error"]

    def test_test_voyageai_key_invalid_format(self, client):
        """Invalid key format rejected with 400 before connectivity test."""
        response = client.post(
            "/api/api-keys/voyageai/test",
            json={"api_key": INVALID_VOYAGEAI_KEY},
        )
        assert response.status_code == 400

    def test_test_voyageai_key_requires_auth(self, unauthenticated_client):
        """Without auth the endpoint rejects the request."""
        response = unauthenticated_client.post(
            "/api/api-keys/voyageai/test",
            json={"api_key": VALID_VOYAGEAI_KEY},
        )
        assert response.status_code in (401, 403, 422, 500)


# ---------------------------------------------------------------------------
# 4. POST /api/api-keys/anthropic/test-configured — Test configured Anthropic key
# ---------------------------------------------------------------------------


class TestTestConfiguredAnthropicKey:
    """POST /api/api-keys/anthropic/test-configured"""

    def test_test_configured_anthropic_key_success(self, client):
        """When key is configured, connectivity test is run and result returned."""
        cfg = _make_config(anthropic_key=VALID_ANTHROPIC_KEY)
        connectivity_result = MagicMock()
        connectivity_result.success = True
        connectivity_result.provider = "anthropic"
        connectivity_result.error = None
        connectivity_result.response_time_ms = 77

        with (
            patch("code_indexer.server.routers.api_keys.get_config_service") as mock_cs,
            patch(
                "code_indexer.server.routers.api_keys.get_api_key_connectivity_tester"
            ) as mock_tester,
        ):
            mock_cs.return_value.load_config.return_value = cfg
            mock_tester.return_value.test_anthropic_connectivity = AsyncMock(
                return_value=connectivity_result
            )

            response = client.post("/api/api-keys/anthropic/test-configured")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "anthropic"

    def test_test_configured_anthropic_key_not_configured(self, client):
        """When no key is configured, returns success=False with descriptive error."""
        cfg = _make_config(anthropic_key="")

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.post("/api/api-keys/anthropic/test-configured")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["provider"] == "anthropic"
        assert data["error"] is not None
        assert "No Anthropic" in data["error"] or "configured" in data["error"].lower()

    def test_test_configured_anthropic_key_requires_auth(self, unauthenticated_client):
        """Without auth the endpoint rejects the request."""
        response = unauthenticated_client.post(
            "/api/api-keys/anthropic/test-configured"
        )
        assert response.status_code in (401, 403, 422, 500)


# ---------------------------------------------------------------------------
# 5. POST /api/api-keys/voyageai/test-configured — Test configured VoyageAI key
# ---------------------------------------------------------------------------


class TestTestConfiguredVoyageaiKey:
    """POST /api/api-keys/voyageai/test-configured"""

    def test_test_configured_voyageai_key_success(self, client):
        """When key is configured, connectivity test is run and result returned."""
        cfg = _make_config(voyageai_key=VALID_VOYAGEAI_KEY)
        connectivity_result = MagicMock()
        connectivity_result.success = True
        connectivity_result.provider = "voyageai"
        connectivity_result.error = None
        connectivity_result.response_time_ms = 55

        with (
            patch("code_indexer.server.routers.api_keys.get_config_service") as mock_cs,
            patch(
                "code_indexer.server.routers.api_keys.get_api_key_connectivity_tester"
            ) as mock_tester,
        ):
            mock_cs.return_value.load_config.return_value = cfg
            mock_tester.return_value.test_voyageai_connectivity = AsyncMock(
                return_value=connectivity_result
            )

            response = client.post("/api/api-keys/voyageai/test-configured")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "voyageai"

    def test_test_configured_voyageai_key_not_configured(self, client):
        """When no key is configured, returns success=False with descriptive error."""
        cfg = _make_config(voyageai_key="")

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.post("/api/api-keys/voyageai/test-configured")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert data["provider"] == "voyageai"
        assert data["error"] is not None
        assert "No VoyageAI" in data["error"] or "configured" in data["error"].lower()

    def test_test_configured_voyageai_key_connectivity_failure(self, client):
        """Connectivity failure returns success=False even if key is configured."""
        cfg = _make_config(voyageai_key=VALID_VOYAGEAI_KEY)
        connectivity_result = MagicMock()
        connectivity_result.success = False
        connectivity_result.provider = "voyageai"
        connectivity_result.error = "Network timeout"
        connectivity_result.response_time_ms = None

        with (
            patch("code_indexer.server.routers.api_keys.get_config_service") as mock_cs,
            patch(
                "code_indexer.server.routers.api_keys.get_api_key_connectivity_tester"
            ) as mock_tester,
        ):
            mock_cs.return_value.load_config.return_value = cfg
            mock_tester.return_value.test_voyageai_connectivity = AsyncMock(
                return_value=connectivity_result
            )

            response = client.post("/api/api-keys/voyageai/test-configured")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "timeout" in data["error"].lower()

    def test_test_configured_voyageai_key_requires_auth(self, unauthenticated_client):
        """Without auth the endpoint rejects the request."""
        response = unauthenticated_client.post("/api/api-keys/voyageai/test-configured")
        assert response.status_code in (401, 403, 422, 500)


# ---------------------------------------------------------------------------
# 6. GET /api/api-keys/status — Get API key configuration status
# ---------------------------------------------------------------------------


class TestGetApiKeysStatus:
    """GET /api/api-keys/status"""

    def test_status_both_configured(self, client):
        """Returns anthropic_configured=True, voyageai_configured=True when both keys set."""
        cfg = _make_config(
            anthropic_key=VALID_ANTHROPIC_KEY, voyageai_key=VALID_VOYAGEAI_KEY
        )

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.get("/api/api-keys/status")

        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_configured"] is True
        assert data["voyageai_configured"] is True

    def test_status_none_configured(self, client):
        """Returns both flags False when no keys are configured."""
        cfg = _make_config(anthropic_key="", voyageai_key="")

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.get("/api/api-keys/status")

        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_configured"] is False
        assert data["voyageai_configured"] is False

    def test_status_only_anthropic_configured(self, client):
        """Returns anthropic_configured=True, voyageai_configured=False."""
        cfg = _make_config(anthropic_key=VALID_ANTHROPIC_KEY, voyageai_key="")

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.get("/api/api-keys/status")

        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_configured"] is True
        assert data["voyageai_configured"] is False

    def test_status_only_voyageai_configured(self, client):
        """Returns anthropic_configured=False, voyageai_configured=True."""
        cfg = _make_config(anthropic_key="", voyageai_key=VALID_VOYAGEAI_KEY)

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.get("/api/api-keys/status")

        assert response.status_code == 200
        data = response.json()
        assert data["anthropic_configured"] is False
        assert data["voyageai_configured"] is True

    def test_status_requires_auth(self, unauthenticated_client):
        """Without auth the endpoint rejects the request."""
        response = unauthenticated_client.get("/api/api-keys/status")
        assert response.status_code in (401, 403, 422, 500)

    def test_status_response_has_correct_fields(self, client):
        """Response body contains exactly the expected fields."""
        cfg = _make_config()

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.get("/api/api-keys/status")

        assert response.status_code == 200
        data = response.json()
        assert set(data.keys()) == {"anthropic_configured", "voyageai_configured"}


# ---------------------------------------------------------------------------
# 7. DELETE /api/api-keys/voyageai — Delete VoyageAI key
# ---------------------------------------------------------------------------


class TestDeleteVoyageaiKey:
    """DELETE /api/api-keys/voyageai"""

    def test_delete_voyageai_key_success(self, client):
        """Configured key is cleared; returns success=True and message."""
        cfg = _make_config(voyageai_key=VALID_VOYAGEAI_KEY)

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg
            mock_cs.return_value.config_manager = MagicMock()

            response = client.delete("/api/api-keys/voyageai")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "voyageai"
        assert "message" in data

    def test_delete_voyageai_key_when_not_configured(self, client):
        """When no key is configured, returns success=True with 'no key' message."""
        cfg = _make_config(voyageai_key="")

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg

            response = client.delete("/api/api-keys/voyageai")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["provider"] == "voyageai"
        # Should indicate there was nothing to clear
        assert "No key" in data["message"] or "configured" in data["message"].lower()

    def test_delete_voyageai_key_clears_config(self, client):
        """After deletion, config is persisted with empty voyageai_api_key."""
        cfg = _make_config(voyageai_key=VALID_VOYAGEAI_KEY)
        mock_config_manager = MagicMock()

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg
            mock_cs.return_value.config_manager = mock_config_manager

            response = client.delete("/api/api-keys/voyageai")

        assert response.status_code == 200
        # Verify save_config was called with cleared key
        mock_config_manager.save_config.assert_called_once()
        saved_config = mock_config_manager.save_config.call_args[0][0]
        assert saved_config.claude_integration_config.voyageai_api_key == ""

    def test_delete_voyageai_key_requires_auth(self, unauthenticated_client):
        """Without auth the endpoint rejects the request."""
        response = unauthenticated_client.delete("/api/api-keys/voyageai")
        assert response.status_code in (401, 403, 422, 500)

    def test_delete_voyageai_key_response_shape(self, client):
        """Response body contains success, provider and message fields."""
        cfg = _make_config(voyageai_key=VALID_VOYAGEAI_KEY)

        with patch(
            "code_indexer.server.routers.api_keys.get_config_service"
        ) as mock_cs:
            mock_cs.return_value.load_config.return_value = cfg
            mock_cs.return_value.config_manager = MagicMock()

            response = client.delete("/api/api-keys/voyageai")

        assert response.status_code == 200
        data = response.json()
        assert "success" in data
        assert "provider" in data
        assert "message" in data
