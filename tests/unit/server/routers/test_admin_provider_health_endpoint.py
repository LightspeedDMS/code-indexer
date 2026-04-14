"""Tests for AC3: GET /admin/provider-health endpoint.

Verifies:
- Returns provider list from ProviderHealthMonitor.get_instance()
- Includes sinbin fields: sinbinned, sinbin_expires_at, sinbin_rounds
- Requires admin JWT authentication (401/403 without auth)
- Handles empty provider list gracefully
- Handles sinbinned provider
- Returns 200 with valid data when authenticated

Bug #679 Part 2.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mock_health_status(
    provider: str = "voyage-ai",
    status: str = "healthy",
    sinbinned: bool = False,
    p50_latency_ms: float = 312.4,
    p95_latency_ms: float = 487.1,
    error_rate: float = 0.02,
    total_requests: int = 1423,
    successful_requests: int = 1394,
    failed_requests: int = 29,
    window_minutes: int = 60,
):
    """Build a mock ProviderHealthStatus object."""
    status_obj = MagicMock()
    status_obj.provider = provider
    status_obj.status = status
    status_obj.sinbinned = sinbinned
    status_obj.p50_latency_ms = p50_latency_ms
    status_obj.p95_latency_ms = p95_latency_ms
    status_obj.error_rate = error_rate
    status_obj.total_requests = total_requests
    status_obj.successful_requests = successful_requests
    status_obj.failed_requests = failed_requests
    status_obj.window_minutes = window_minutes
    return status_obj


def _make_mock_monitor(
    providers: dict = None,
    sinbin_until: dict = None,
    sinbin_rounds: dict = None,
):
    """Build a mock ProviderHealthMonitor with controllable state."""
    monitor = MagicMock()
    providers = providers or {}
    _sinbin_until = sinbin_until or {}
    _sinbin_rounds = sinbin_rounds or {}
    monitor.get_health.return_value = providers
    monitor._sinbin_until = _sinbin_until
    monitor._sinbin_rounds = _sinbin_rounds
    monitor.is_sinbinned = MagicMock(
        side_effect=lambda p: time.monotonic() < _sinbin_until.get(p, 0.0)
    )
    monitor.get_sinbin_rounds = MagicMock(
        side_effect=lambda p: _sinbin_rounds.get(p, 0)
    )
    monitor.get_sinbin_ttl_seconds = MagicMock(
        side_effect=lambda p: (
            max(0.0, _sinbin_until[p] - time.monotonic())
            if p in _sinbin_until and time.monotonic() < _sinbin_until[p]
            else None
        )
    )
    return monitor


@pytest.fixture()
def app_with_router():
    """Create a minimal FastAPI app with the admin_provider_health router."""
    from code_indexer.server.routers.admin_provider_health import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def client(app_with_router):
    return TestClient(app_with_router, raise_server_exceptions=False)


@pytest.fixture()
def authed_client(app_with_router):
    """TestClient with admin auth dependency overridden to return a mock admin user."""
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    mock_admin = MagicMock()
    mock_admin.username = "admin"
    mock_admin.is_admin = True

    app_with_router.dependency_overrides[get_current_admin_user_hybrid] = (
        lambda: mock_admin
    )
    return TestClient(app_with_router, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


class TestProviderHealthEndpointAuth:
    """GET /admin/provider-health must require admin authentication."""

    def test_endpoint_requires_admin_auth_returns_401_or_403(self, app_with_router):
        """Auth dependency is wired: endpoint returns 401/403 when auth raises HTTPException."""
        from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

        def _raise_forbidden():
            raise HTTPException(status_code=403, detail="Forbidden")

        app_with_router.dependency_overrides[get_current_admin_user_hybrid] = (
            _raise_forbidden
        )
        try:
            with TestClient(
                app_with_router, raise_server_exceptions=False
            ) as test_client:
                response = test_client.get("/admin/provider-health")
                assert response.status_code in (401, 403)
        finally:
            app_with_router.dependency_overrides.pop(
                get_current_admin_user_hybrid, None
            )

    def test_endpoint_route_exists(self, app_with_router):
        """Router registers /admin/provider-health route."""
        routes = [r.path for r in app_with_router.routes]
        assert "/admin/provider-health" in routes


# ---------------------------------------------------------------------------
# Happy path: provider list returned
# ---------------------------------------------------------------------------


class TestProviderHealthEndpointHappyPath:
    """GET /admin/provider-health returns valid provider list."""

    def test_endpoint_returns_200_with_valid_data(self, authed_client):
        """Authenticated request returns HTTP 200."""
        voyage_status = _make_mock_health_status("voyage-ai")
        monitor = _make_mock_monitor(
            providers={"voyage-ai": voyage_status},
        )
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        assert response.status_code == 200

    def test_endpoint_returns_providers_list(self, authed_client):
        """Response body contains 'providers' key with list of provider dicts."""
        voyage_status = _make_mock_health_status("voyage-ai", status="healthy")
        monitor = _make_mock_monitor(providers={"voyage-ai": voyage_status})
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        data = response.json()
        assert "providers" in data
        assert isinstance(data["providers"], list)
        assert len(data["providers"]) == 1
        assert data["providers"][0]["provider"] == "voyage-ai"

    def test_endpoint_includes_standard_health_fields(self, authed_client):
        """Each provider entry includes status, latency, error_rate, and request counts."""
        voyage_status = _make_mock_health_status(
            "voyage-ai",
            status="healthy",
            p50_latency_ms=312.4,
            p95_latency_ms=487.1,
            error_rate=0.02,
            total_requests=1423,
            successful_requests=1394,
            failed_requests=29,
            window_minutes=60,
        )
        monitor = _make_mock_monitor(providers={"voyage-ai": voyage_status})
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        entry = response.json()["providers"][0]
        assert entry["status"] == "healthy"
        assert entry["p50_latency_ms"] == pytest.approx(312.4)
        assert entry["p95_latency_ms"] == pytest.approx(487.1)
        assert entry["error_rate"] == pytest.approx(0.02)
        assert entry["total_requests"] == 1423
        assert entry["successful_requests"] == 1394
        assert entry["failed_requests"] == 29
        assert entry["window_minutes"] == 60

    def test_endpoint_multiple_providers_returned(self, authed_client):
        """Response includes all providers tracked by the monitor."""
        providers = {
            "voyage-ai": _make_mock_health_status("voyage-ai"),
            "cohere": _make_mock_health_status("cohere", status="degraded"),
        }
        monitor = _make_mock_monitor(providers=providers)
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        data = response.json()
        provider_names = {p["provider"] for p in data["providers"]}
        assert "voyage-ai" in provider_names
        assert "cohere" in provider_names


# ---------------------------------------------------------------------------
# Sinbin fields
# ---------------------------------------------------------------------------


class TestProviderHealthEndpointSinbinFields:
    """Sinbin circuit-breaker fields must be included in each provider entry."""

    def test_endpoint_includes_sinbinned_false_for_healthy_provider(
        self, authed_client
    ):
        """Non-sinbinned provider has sinbinned=false, sinbin_expires_at=null, sinbin_rounds=0."""
        voyage_status = _make_mock_health_status("voyage-ai", sinbinned=False)
        monitor = _make_mock_monitor(
            providers={"voyage-ai": voyage_status},
            sinbin_until={},
            sinbin_rounds={"voyage-ai": 0},
        )
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        entry = response.json()["providers"][0]
        assert entry["sinbinned"] is False
        assert entry["sinbin_expires_at"] is None
        assert entry["sinbin_rounds"] == 0

    def test_endpoint_includes_sinbin_fields_for_sinbinned_provider(
        self, authed_client
    ):
        """Sinbinned provider has sinbinned=true, sinbin_expires_at set, sinbin_rounds > 0."""
        future_expiry = time.monotonic() + 300.0
        voyage_status = _make_mock_health_status("voyage-ai", sinbinned=True)
        monitor = _make_mock_monitor(
            providers={"voyage-ai": voyage_status},
            sinbin_until={"voyage-ai": future_expiry},
            sinbin_rounds={"voyage-ai": 2},
        )
        monitor.is_sinbinned = MagicMock(return_value=True)
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        entry = response.json()["providers"][0]
        assert entry["sinbinned"] is True
        assert entry["sinbin_rounds"] == 2
        # sinbin_expires_at should be a non-null value (seconds remaining or timestamp)
        assert entry["sinbin_expires_at"] is not None

    def test_endpoint_handles_sinbinned_provider_status_field(self, authed_client):
        """Sinbinned provider status field reflects sinbinned state."""
        voyage_status = _make_mock_health_status(
            "voyage-ai", status="sinbinned", sinbinned=True
        )
        monitor = _make_mock_monitor(
            providers={"voyage-ai": voyage_status},
            sinbin_until={"voyage-ai": time.monotonic() + 120.0},
            sinbin_rounds={"voyage-ai": 1},
        )
        monitor.is_sinbinned = MagicMock(return_value=True)
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        entry = response.json()["providers"][0]
        assert entry["sinbinned"] is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestProviderHealthEndpointEdgeCases:
    """Edge cases: empty provider list, monitor not configured."""

    def test_endpoint_handles_no_providers_configured(self, authed_client):
        """Empty provider list returns 200 with providers=[]."""
        monitor = _make_mock_monitor(providers={})
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        assert response.status_code == 200
        assert response.json()["providers"] == []

    def test_endpoint_provider_entry_has_all_required_keys(self, authed_client):
        """Each provider entry contains all keys specified in AC3."""
        voyage_status = _make_mock_health_status("voyage-ai")
        monitor = _make_mock_monitor(
            providers={"voyage-ai": voyage_status},
            sinbin_rounds={"voyage-ai": 0},
        )
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        entry = response.json()["providers"][0]
        required_keys = {
            "provider",
            "status",
            "sinbinned",
            "sinbin_expires_at",
            "sinbin_rounds",
            "p50_latency_ms",
            "p95_latency_ms",
            "error_rate",
            "total_requests",
            "successful_requests",
            "failed_requests",
            "window_minutes",
        }
        missing = required_keys - set(entry.keys())
        assert not missing, f"Missing keys in provider entry: {missing}"

    def test_endpoint_sinbin_rounds_defaults_to_zero_when_not_in_monitor(
        self, authed_client
    ):
        """sinbin_rounds defaults to 0 when provider not in monitor._sinbin_rounds."""
        voyage_status = _make_mock_health_status("voyage-ai")
        monitor = _make_mock_monitor(
            providers={"voyage-ai": voyage_status},
            sinbin_rounds={},  # provider not in rounds dict
        )
        with patch(
            "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
            return_value=monitor,
        ):
            response = authed_client.get("/admin/provider-health")
        entry = response.json()["providers"][0]
        assert entry["sinbin_rounds"] == 0
