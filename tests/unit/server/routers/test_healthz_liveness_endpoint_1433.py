"""
Regression tests for the Bug #1433 follow-up: /health and /api/system/health
both require authentication, so an unauthenticated HAProxy `httpchk` probe
never sees the real health JSON -- the golden-repos storage-failure signal
added for Bug #1433 never reaches the load balancer's up/down decision.

These tests exercise the new public, UNAUTHENTICATED `GET /healthz`
liveness/readiness endpoint:
  - No Authorization header required at all.
  - Response body exposes ONLY {"status": ...} -- no services/system/
    failure_reasons/file-path detail (minimal information disclosure).
  - HTTP status code itself maps healthy/degraded -> 200, unhealthy -> 503,
    so a load balancer configured with a plain `option httpchk GET /healthz`
    (no body-parsing `http-check expect`) still routes correctly.
  - A raised exception from the underlying health service fails safe:
    a definitive response, no traceback leakage, no unhandled 500.

Also proves the existing authenticated /health and /api/system/health
routes are completely unchanged (no regression to their auth or shape).
Authenticated-shape checks use FastAPI's standard
`app.dependency_overrides` mechanism to bypass real login -- no
credentials of any kind appear in this file.
"""

import importlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.code_indexer.server.app import create_app
from src.code_indexer.server.auth.user_manager import User, UserRole
from src.code_indexer.server.models.api_models import (
    HealthCheckResponse,
    HealthStatus,
    ServiceHealthInfo,
    SystemHealthInfo,
)

# Named constants -- no magic numbers/inline literals in test bodies
NORMAL_MEMORY_PERCENT = 20.0
NORMAL_CPU_PERCENT = 20.0
NORMAL_DISK_FREE_GB = 200.0
HEALTHZ_PATH = "/healthz"
# NOTE: no "src." prefix here. create_app()'s internal route-wiring chain
# imports inline_misc/health_service under the unprefixed `code_indexer.*`
# module namespace (a pre-existing dual-import-path quirk in this
# codebase), so patching the "src."-prefixed path would silently patch an
# unused module object that the running app never actually references.
HEALTH_SERVICE_PATCH_TARGET = "code_indexer.server.routers.inline_misc.health_service"
# Same dual-import-path rationale as HEALTH_SERVICE_PATCH_TARGET above: the
# running app's route closures reference the unprefixed `code_indexer.*`
# module object, so the TTL-cache reset/patch targets below must resolve
# against that same module, not a "src."-prefixed duplicate.
INLINE_MISC_MODULE_PATH = "code_indexer.server.routers.inline_misc"


@pytest.fixture(autouse=True)
def _reset_healthz_ttl_cache():
    """Ensures the /healthz short-TTL cache (module-level state) never
    leaks a cached status across test cases -- each test that hits
    /healthz must observe the fresh HEALTH_SERVICE_PATCH_TARGET mock it
    itself configured, not a cached value left over from a prior test."""
    inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
    inline_misc._reset_healthz_cache()
    yield
    inline_misc._reset_healthz_cache()


def _service_health(status_value: HealthStatus) -> ServiceHealthInfo:
    return ServiceHealthInfo(
        status=status_value, response_time_ms=1, error_message=None
    )


def _system_info() -> SystemHealthInfo:
    return SystemHealthInfo(
        memory_usage_percent=NORMAL_MEMORY_PERCENT,
        cpu_usage_percent=NORMAL_CPU_PERCENT,
        active_jobs=0,
        disk_free_space_gb=NORMAL_DISK_FREE_GB,
        disk_read_kb_s=0.0,
        disk_write_kb_s=0.0,
        net_rx_kb_s=0.0,
        net_tx_kb_s=0.0,
    )


def _health_response(status_value: HealthStatus) -> HealthCheckResponse:
    return HealthCheckResponse(
        status=status_value,
        timestamp=datetime.now(timezone.utc),
        services={
            "database": _service_health(status_value),
            "storage": _service_health(status_value),
        },
        system=_system_info(),
        failure_reasons=(
            [] if status_value == HealthStatus.HEALTHY else ["some failure detail"]
        ),
    )


def _build_client() -> TestClient:
    return TestClient(create_app())


def _stub_authenticated_user() -> User:
    """Stand-in User returned by the overridden get_current_user dependency
    -- proves the authenticated response shape without any real login flow
    or credentials of any kind."""
    return User(
        username="stub-test-user",
        password_hash="unused",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


def _override_get_current_user_for_route(app, path: str, method: str = "GET") -> None:
    """Override the auth dependency for a specific route using the exact
    callable object FastAPI registered on that route's Dependant.

    This project's test/app modules are reachable under two distinct
    import roots (`src.code_indexer...` and `code_indexer...`), which can
    produce two separate module objects for the same source file -- so a
    dependency looked up via a fresh `import` does not `is`-match the
    callable the route actually holds. Grabbing the callable directly off
    the route sidesteps that entirely; no credentials are involved.
    """
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(
            route, "methods", set()
        ):
            for dependency in route.dependant.dependencies:
                if dependency.call.__name__ == "get_current_user":
                    app.dependency_overrides[dependency.call] = _stub_authenticated_user
                    return
            raise AssertionError(
                f"No get_current_user dependency found on route {method} {path}"
            )
    raise AssertionError(f"Route {method} {path} not found on app")


class TestHealthzStatusMapping:
    """GET /healthz maps the computed overall status to both the HTTP
    status code and a minimal JSON body."""

    def test_healthz_healthy_returns_200_with_minimal_body(self):
        client = _build_client()

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.HEALTHY
            )
            # No Authorization header at all -- genuinely public endpoint.
            response = client.get(HEALTHZ_PATH)

        assert response.status_code == 200
        body = response.json()
        # Exact key set -- proves no extra fields (services/system/
        # failure_reasons/etc.) leaked to the unauthenticated caller.
        assert set(body.keys()) == {"status"}
        assert body["status"] == "healthy"

    def test_healthz_degraded_returns_200_with_minimal_body(self):
        """Degraded is still serviceable -- must NOT be drained (200, not 503)."""
        client = _build_client()

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.DEGRADED
            )
            response = client.get(HEALTHZ_PATH)

        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {"status"}
        assert body["status"] == "degraded"

    def test_healthz_unhealthy_returns_503_with_minimal_body(self):
        client = _build_client()

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.UNHEALTHY
            )
            response = client.get(HEALTHZ_PATH)

        assert response.status_code == 503
        body = response.json()
        assert set(body.keys()) == {"status"}
        assert body["status"] == "unhealthy"


class TestHealthzSafetyAndAuth:
    """GET /healthz requires no auth at all and fails safe on error."""

    def test_healthz_no_authorization_header_required(self):
        """The route must not depend on dependencies.get_current_user at
        all -- a bare request with zero auth-related headers must succeed."""
        client = _build_client()

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.HEALTHY
            )
            response = client.get(HEALTHZ_PATH, headers={})

        assert response.status_code != 401
        assert response.status_code != 403

    def test_healthz_underlying_exception_fails_safe(self):
        """If HealthCheckService.get_system_health() itself raises, the
        liveness probe must still return a definitive response -- no raw
        traceback/exception text leaked to the unauthenticated caller, and
        no unhandled 500."""
        client = _build_client()

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.side_effect = RuntimeError(
                "simulated total health-service failure with secret detail /var/lib/cidx/secret-path"
            )
            response = client.get(HEALTHZ_PATH)

        # Must be a definitive, well-formed response -- not a raw 500 crash.
        assert response.status_code == 503
        raw_text = response.text
        assert "Traceback" not in raw_text
        assert "RuntimeError" not in raw_text
        assert "secret-path" not in raw_text

        body = response.json()
        assert set(body.keys()) == {"status"}
        assert body["status"] == "unhealthy"


class TestHealthzTTLCache:
    """Security fix: /healthz is unauthenticated AND exempted from
    AdmissionControlMiddleware (any path starting with "/health" bypasses
    the global backpressure mechanism), so a naive per-request call to
    health_service.get_system_health() lets an unauthenticated flood
    trigger unbounded subprocess forks + DB connections. A short-TTL,
    process-local cache collapses a flood into ~one real computation per
    TTL window."""

    def test_healthz_second_call_within_ttl_uses_cache_not_recomputed(self):
        """Two rapid successive calls within the TTL window must result in
        exactly ONE real health_service.get_system_health() call -- the
        second call is served from the cache."""
        client = _build_client()

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.HEALTHY
            )
            first = client.get(HEALTHZ_PATH)
            second = client.get(HEALTHZ_PATH)

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["status"] == "healthy"
        assert second.json()["status"] == "healthy"
        assert mock_health_svc.get_system_health.call_count == 1

    def test_healthz_call_after_ttl_expiry_triggers_fresh_call(self):
        """Once the TTL has elapsed, the next call must trigger a fresh
        health_service.get_system_health() call rather than reusing the
        (now-stale) cached value. Controls elapsed time deterministically
        via the narrow `_healthz_monotonic` alias (NOT the process-global
        time.monotonic, which the ASGI test client's event loop also
        depends on -- patching that globally hangs the loop) rather than a
        real sleep."""
        client = _build_client()

        with (
            patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc,
            patch(
                f"{INLINE_MISC_MODULE_PATH}._healthz_monotonic",
                side_effect=[0.0, 100.0],
            ),
        ):
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.HEALTHY
            )
            first = client.get(HEALTHZ_PATH)
            second = client.get(HEALTHZ_PATH)

        assert first.status_code == 200
        assert second.status_code == 200
        assert mock_health_svc.get_system_health.call_count == 2

    def test_healthz_reflects_status_change_after_ttl_expiry(self):
        """The cache must never mask a genuine status change: a call
        served from the cache (within TTL) must keep returning the OLD
        status, while the first call after TTL expiry must reflect the
        NEW status -- proving this is a bounded-freshness cache, not a
        stuck/over-cached value."""
        client = _build_client()

        with (
            patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc,
            patch(
                f"{INLINE_MISC_MODULE_PATH}._healthz_monotonic",
                side_effect=[0.0, 0.5, 100.0],
            ),
        ):
            mock_health_svc.get_system_health.side_effect = [
                _health_response(HealthStatus.HEALTHY),
                _health_response(HealthStatus.UNHEALTHY),
            ]

            first = client.get(HEALTHZ_PATH)  # miss @ t=0.0 -> healthy, cached
            second = client.get(HEALTHZ_PATH)  # hit @ t=0.5 -> still healthy
            assert mock_health_svc.get_system_health.call_count == 1

            third = client.get(HEALTHZ_PATH)  # miss @ t=100.0 -> unhealthy, fresh

        assert first.status_code == 200
        assert first.json()["status"] == "healthy"
        assert second.status_code == 200
        assert second.json()["status"] == "healthy"
        assert third.status_code == 503
        assert third.json()["status"] == "unhealthy"
        assert mock_health_svc.get_system_health.call_count == 2


class TestExistingHealthRoutesUnchanged:
    """Regression: /health and /api/system/health must be completely
    unaffected by the new /healthz route -- same auth requirement, same
    response shape."""

    def test_health_route_still_requires_auth(self):
        client = _build_client()

        response = client.get("/health")

        assert response.status_code in (401, 403)

    def test_api_system_health_route_still_requires_auth(self):
        client = _build_client()

        response = client.get("/api/system/health")

        assert response.status_code in (401, 403)

    def test_authenticated_routes_retain_full_response_shape(self):
        """With the auth dependency overridden (standard FastAPI test
        technique -- no credentials involved), both /health and
        /api/system/health must still return their full, pre-existing
        detailed shapes. Proves the new /healthz route did not strip any
        field from either authenticated endpoint."""
        app = create_app()
        _override_get_current_user_for_route(app, "/health")
        _override_get_current_user_for_route(app, "/api/system/health")
        client = TestClient(app)

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.HEALTHY
            )

            health_response = client.get("/health")
            system_health_response = client.get("/api/system/health")

        app.dependency_overrides.clear()

        assert health_response.status_code == 200
        health_body = health_response.json()
        assert "status" in health_body
        assert "uptime" in health_body
        assert "job_queue" in health_body
        assert "maintenance_mode" in health_body

        assert system_health_response.status_code == 200
        system_health_body = system_health_response.json()
        assert "services" in system_health_body
        assert "system" in system_health_body
        assert "failure_reasons" in system_health_body

    def test_health_and_system_health_remain_uncached_each_call(self):
        """Regression: the /healthz short-TTL cache introduced for the
        unauthenticated liveness probe must NOT leak into /health or
        /api/system/health -- both explicitly document "uncached for
        real-time data" as an intentional design decision for these
        authenticated diagnostic endpoints. Two rapid calls to
        /api/system/health must each invoke get_system_health() fresh."""
        app = create_app()
        _override_get_current_user_for_route(app, "/api/system/health")
        client = TestClient(app)

        with patch(HEALTH_SERVICE_PATCH_TARGET) as mock_health_svc:
            mock_health_svc.get_system_health.return_value = _health_response(
                HealthStatus.HEALTHY
            )
            first = client.get("/api/system/health")
            second = client.get("/api/system/health")

        app.dependency_overrides.clear()

        assert first.status_code == 200
        assert second.status_code == 200
        assert mock_health_svc.get_system_health.call_count == 2
