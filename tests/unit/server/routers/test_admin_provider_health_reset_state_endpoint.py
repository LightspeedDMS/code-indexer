"""Tests for POST /admin/provider-health/reset-state endpoint (Bug #902).

Verifies:
- Route is registered at /admin/provider-health/reset-state with POST method
- Requires admin JWT authentication: 403 (non-admin) and 401 (unauthenticated)
- Returns HTTP 200 on success
- Calls clear_health_state_all() on the ProviderHealthMonitor singleton exactly once
- Does NOT call clear_sinbin_all() (separate concern, narrow semantics preserved)
- Response body contains a "reset" key confirming the operation
- Idempotent: multiple calls all return 200 and each calls clear_health_state_all()

Distinct from /admin/provider-health/clear-sinbin: that endpoint only clears sinbin
cooldown timers (narrow operational semantics). This endpoint wipes ALL rolling health
state including metrics, consecutive-failure counters, windowed failure deques, and
stops active recovery probes. Required for Phase 5 E2E test isolation where
_compute_status() returns 'down' even after sinbin is cleared (Bug #902 root cause).

Auth tests use dependency overrides (not the real DB) because the auth dependency
requires a live database to resolve JWT tokens -- unavailable in unit tests.

Monitor isolation uses patch() for ProviderHealthMonitor.get_instance(), matching the
established project convention in test_admin_provider_health_endpoint.py where all
existing clear-sinbin tests use the same patch() pattern (no FastAPI dependency seam
exists for the monitor singleton in the production router).
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi import status as http_status
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_with_router():
    """Create a minimal FastAPI app with the admin_provider_health router."""
    from code_indexer.server.routers.admin_provider_health import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def authed_client(app_with_router):
    """TestClient with admin auth dependency overridden; closed after test via yield."""
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    mock_admin = MagicMock()
    mock_admin.username = "admin"
    mock_admin.is_admin = True

    app_with_router.dependency_overrides[get_current_admin_user_hybrid] = (
        lambda: mock_admin
    )
    with TestClient(app_with_router, raise_server_exceptions=False) as client:
        yield client


@contextmanager
def _patched_monitor():
    """Context manager yielding a mock ProviderHealthMonitor with clear stubs.

    Uses patch() matching established project convention: the production router
    calls ProviderHealthMonitor.get_instance() directly without a FastAPI
    dependency seam, so all tests in this router's test suite use patch().
    """
    monitor = MagicMock()
    monitor.clear_health_state_all = MagicMock()
    monitor.clear_sinbin_all = MagicMock()
    with patch(
        "code_indexer.server.routers.admin_provider_health.ProviderHealthMonitor.get_instance",
        return_value=monitor,
    ):
        yield monitor


# ---------------------------------------------------------------------------
# Auth test helper -- shared mechanics factored out of parametrized cases
# ---------------------------------------------------------------------------


def _assert_auth_rejection(app_with_router, expected_status: int, detail: str) -> None:
    """Override auth dependency to raise HTTPException and assert the exact status code.

    Parametrized auth tests delegate to this helper to avoid duplicating the
    override/TestClient/request/cleanup sequence.
    """
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    def _raise():
        raise HTTPException(status_code=expected_status, detail=detail)

    app_with_router.dependency_overrides[get_current_admin_user_hybrid] = _raise
    try:
        with TestClient(app_with_router, raise_server_exceptions=False) as client:
            response = client.post("/admin/provider-health/reset-state")
            assert response.status_code == expected_status
    finally:
        app_with_router.dependency_overrides.pop(get_current_admin_user_hybrid, None)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestResetStateRouteRegistered:
    """POST /admin/provider-health/reset-state must be registered as a POST route."""

    def test_reset_state_route_exists_with_post_method(self, app_with_router):
        """Router registers /admin/provider-health/reset-state with POST method.

        Checks both path and method to reject misconfigured GET-only routes.
        """
        matching = [
            r
            for r in app_with_router.routes
            if getattr(r, "path", None) == "/admin/provider-health/reset-state"
        ]
        assert matching, "Route /admin/provider-health/reset-state is not registered"
        route = matching[0]
        assert "POST" in route.methods, (
            f"Route /admin/provider-health/reset-state is registered but not for POST; "
            f"methods={route.methods}"
        )


# ---------------------------------------------------------------------------
# Auth enforcement -- parametrized over the two distinct rejection paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected_status,detail",
    [
        (http_status.HTTP_403_FORBIDDEN, "Forbidden"),
        (http_status.HTTP_401_UNAUTHORIZED, "Unauthorized"),
    ],
)
def test_endpoint_rejects_with_correct_auth_status(
    app_with_router, expected_status, detail
):
    """Auth dependency wired to endpoint: exact rejection status propagates to client.

    Two parametrized cases prove both auth rejection paths:
    - 403 Forbidden: non-admin or banned user
    - 401 Unauthorized: missing or invalid token

    Dependency override is the only option because the real auth dependency
    requires a live database -- unavailable in unit tests.
    """
    _assert_auth_rejection(app_with_router, expected_status, detail)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestResetStateEndpointHappyPath:
    """POST /admin/provider-health/reset-state returns 200 and calls clear_health_state_all."""

    def test_returns_200_on_success(self, authed_client):
        """Authenticated POST returns HTTP 200."""
        with _patched_monitor():
            response = authed_client.post("/admin/provider-health/reset-state")
        assert response.status_code == http_status.HTTP_200_OK

    def test_calls_clear_health_state_all_exactly_once(self, authed_client):
        """Handler calls clear_health_state_all() on the monitor exactly once."""
        with _patched_monitor() as monitor:
            authed_client.post("/admin/provider-health/reset-state")
        monitor.clear_health_state_all.assert_called_once()

    def test_does_not_call_clear_sinbin_all(self, authed_client):
        """reset-state must NOT call clear_sinbin_all() -- separate narrow semantics preserved.

        Operators relying on /clear-sinbin for operational sinbin management must not
        be affected by /reset-state. These are distinct operations with distinct scopes.
        """
        with _patched_monitor() as monitor:
            authed_client.post("/admin/provider-health/reset-state")
        monitor.clear_sinbin_all.assert_not_called()

    def test_response_body_contains_reset_key(self, authed_client):
        """Response body contains a 'reset' key confirming the operation completed."""
        with _patched_monitor():
            response = authed_client.post("/admin/provider-health/reset-state")
        data = response.json()
        assert "reset" in data


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestResetStateEndpointIdempotency:
    """Multiple POST calls to /admin/provider-health/reset-state must all succeed."""

    def test_double_post_returns_200_both_times(self, authed_client):
        """Two consecutive POSTs both return HTTP 200."""
        with _patched_monitor():
            response1 = authed_client.post("/admin/provider-health/reset-state")
            response2 = authed_client.post("/admin/provider-health/reset-state")
        assert response1.status_code == http_status.HTTP_200_OK
        assert response2.status_code == http_status.HTTP_200_OK

    def test_double_post_calls_clear_health_state_all_twice(self, authed_client):
        """Two consecutive POSTs call clear_health_state_all() twice (once per call)."""
        with _patched_monitor() as monitor:
            authed_client.post("/admin/provider-health/reset-state")
            authed_client.post("/admin/provider-health/reset-state")
        assert monitor.clear_health_state_all.call_count == 2
