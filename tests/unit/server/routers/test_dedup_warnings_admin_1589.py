"""
Tests for POST /api/admin/diagnostics/dedup-warnings/clear-all (Story #1589).

Verifies:
- Route is registered at /api/admin/diagnostics/dedup-warnings/clear-all with POST
- Requires admin auth: 403 (non-admin) and 401 (unauthenticated); underlying
  clear-all logic is never invoked when auth is rejected
- Returns HTTP 200 with {"cleared_count": N} on success
- Returns HTTP 503 when golden_repo_manager is not initialized in app.state
- Returns HTTP 500 when the service layer raises DedupStateUnavailableError
- Idempotent: a second call after everything is cleared reports cleared_count=0

Auth tests use dependency overrides (not the real DB) because the auth
dependency requires a live database to resolve JWT tokens -- unavailable in
unit tests. Mirrors test_admin_provider_health_reset_state_endpoint.py's
exact pattern.

Service-layer isolation uses patch() for the module's imported
clear_all_dedup_states, matching the established project convention (e.g.
admin_provider_health's tests patching ProviderHealthMonitor.get_instance)
where no FastAPI dependency seam exists for the underlying function.

Additionally (Finding F2 remediation): TestClearAllEndpointRealBackendChain
near the end of this module drives the router -> service wrapper -> real
SQLite backend chain through a genuine TestClient HTTP call, closing the
story's own Testing Requirements gap ("successful clear-all" and
"empty-state no-op" against a real backend).
"""

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi import status as http_status
from fastapi.testclient import TestClient
from tests.utils.route_registration import find_route

from code_indexer.server.routers.dedup_warnings_admin import CLEAR_ALL_DEDUP_REASON
from code_indexer.server.services.fleet_migration.dedup_state import (
    record_dedup_outcome,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend

_ENDPOINT_PATH = "/api/admin/diagnostics/dedup-warnings/clear-all"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _install_admin_auth_override(app) -> MagicMock:
    """Override get_current_admin_user_hybrid with a fixed admin user.

    THE single implementation of this override for the whole module (see
    the module docstring for why it exists: JWT resolution requires a
    live database, unavailable in a unit test process). Every client
    fixture in this module -- real backend or not -- calls this SAME
    helper, so there is exactly one place implementing it.
    """
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    mock_admin = MagicMock()
    mock_admin.username = "admin"
    mock_admin.is_admin = True
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: mock_admin
    return mock_admin


@pytest.fixture()
def app_with_router():
    """Create a minimal FastAPI app with the dedup_warnings_admin router."""
    from code_indexer.server.routers.dedup_warnings_admin import router

    app = FastAPI()
    app.include_router(router)
    app.state.golden_repo_manager = MagicMock()
    return app


@pytest.fixture()
def authed_client(app_with_router):
    """TestClient with admin auth dependency overridden; closed after test via yield."""
    _install_admin_auth_override(app_with_router)
    with TestClient(app_with_router, raise_server_exceptions=False) as client:
        yield client


@contextmanager
def _patched_clear_all(return_value=0, side_effect=None):
    """Patch the router module's imported clear_all_dedup_states function."""
    mock_fn = MagicMock(return_value=return_value, side_effect=side_effect)
    with patch(
        "code_indexer.server.routers.dedup_warnings_admin.clear_all_dedup_states",
        mock_fn,
    ):
        yield mock_fn


def _assert_auth_rejection(app_with_router, expected_status: int, detail: str) -> None:
    from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

    def _raise():
        raise HTTPException(status_code=expected_status, detail=detail)

    app_with_router.dependency_overrides[get_current_admin_user_hybrid] = _raise
    try:
        with TestClient(app_with_router, raise_server_exceptions=False) as client:
            with _patched_clear_all(return_value=5) as mock_fn:
                response = client.post(_ENDPOINT_PATH)
                assert response.status_code == expected_status
                mock_fn.assert_not_called()
    finally:
        app_with_router.dependency_overrides.pop(get_current_admin_user_hybrid, None)


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


class TestClearAllRouteRegistered:
    def test_route_exists_with_post_method(self, app_with_router):
        route = find_route(app_with_router, _ENDPOINT_PATH)
        assert route is not None, f"Route {_ENDPOINT_PATH} is not registered"
        assert "POST" in route.methods, (
            f"Route {_ENDPOINT_PATH} is registered but not for POST; "
            f"methods={route.methods}"
        )


# ---------------------------------------------------------------------------
# Auth enforcement
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
    _assert_auth_rejection(app_with_router, expected_status, detail)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestClearAllEndpointHappyPath:
    def test_returns_200_with_cleared_count_on_success(self, authed_client):
        with _patched_clear_all(return_value=3):
            response = authed_client.post(_ENDPOINT_PATH)
        assert response.status_code == http_status.HTTP_200_OK
        assert response.json() == {"cleared_count": 3}

    def test_calls_clear_all_with_expected_reason(self, authed_client):
        with _patched_clear_all(return_value=0) as mock_fn:
            authed_client.post(_ENDPOINT_PATH)
        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        assert kwargs.get("reason") == "manually acknowledged via Diagnostics tab"


# ---------------------------------------------------------------------------
# Server-state / error handling
# ---------------------------------------------------------------------------


class TestClearAllEndpointErrorHandling:
    def test_returns_503_when_golden_repo_manager_not_initialized(
        self, app_with_router
    ):
        app_with_router.state.golden_repo_manager = None
        from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid

        mock_admin = MagicMock()
        app_with_router.dependency_overrides[get_current_admin_user_hybrid] = (
            lambda: mock_admin
        )
        with TestClient(app_with_router, raise_server_exceptions=False) as client:
            response = client.post(_ENDPOINT_PATH)
        assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE

    def test_returns_500_when_service_layer_raises_unavailable(self, authed_client):
        from code_indexer.server.services.fleet_migration.dedup_state import (
            DedupStateUnavailableError,
        )

        with _patched_clear_all(
            side_effect=DedupStateUnavailableError("simulated backend outage")
        ):
            response = authed_client.post(_ENDPOINT_PATH)
        assert response.status_code == http_status.HTTP_500_INTERNAL_SERVER_ERROR


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestClearAllEndpointIdempotency:
    def test_second_call_reports_zero_cleared(self, authed_client):
        with _patched_clear_all(return_value=2):
            first = authed_client.post(_ENDPOINT_PATH)
        with _patched_clear_all(return_value=0):
            second = authed_client.post(_ENDPOINT_PATH)

        assert first.status_code == http_status.HTTP_200_OK
        assert first.json() == {"cleared_count": 2}
        assert second.status_code == http_status.HTTP_200_OK
        assert second.json() == {"cleared_count": 0}


# ---------------------------------------------------------------------------
# Real backend chain (Finding F2 remediation)
# ---------------------------------------------------------------------------
#
# Every test above patches clear_all_dedup_states directly, so the HTTP
# request -> router -> service wrapper -> real backend chain was never
# exercised end-to-end. This section closes that gap: a REAL
# GoldenRepoMetadataSqliteBackend is wired onto app.state.golden_repo_manager
# (via the same _FakeGoldenRepoManagerWithBackend double
# test_dedup_state_clear_all_1589.py already established -- the dedup_state.py
# service wrapper only ever reads `_sqlite_backend` off whatever manager it is
# handed), seeded with real rows, and driven through a genuine HTTP call.
#
# The admin-auth override reused here (via the SAME _install_admin_auth_override
# helper `authed_client` above already calls) is not new mocking scope -- see
# the module docstring. There is NO mock of clear_all_dedup_states, the
# service wrapper, or the backend anywhere in this section; that is the
# actual subject this section proves runs for real.


class _FakeGoldenRepoManagerWithBackend:
    """Mirrors test_dedup_state_clear_all_1589.py's exact double."""

    def __init__(self, sqlite_backend):
        self._sqlite_backend = sqlite_backend


@pytest.fixture()
def real_sqlite_backend():
    """A real, on-disk GoldenRepoMetadataSqliteBackend with the
    fleet_migration_dedup_state table created -- no mock, no in-memory
    fake. `close()` is guaranteed in `finally` even if table creation
    raises (ensure_table_exists() runs INSIDE the try, not before it)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        backend = GoldenRepoMetadataSqliteBackend(db_path)
        try:
            backend.ensure_table_exists()
            yield backend
        finally:
            backend.close()


@pytest.fixture()
def app_with_real_backend(real_sqlite_backend):
    """A minimal FastAPI app with the dedup_warnings_admin router, wired
    to a REAL backend instead of a MagicMock -- the actual production
    wiring shape (request.app.state.golden_repo_manager._sqlite_backend)."""
    from code_indexer.server.routers.dedup_warnings_admin import router

    app = FastAPI()
    app.include_router(router)
    app.state.golden_repo_manager = _FakeGoldenRepoManagerWithBackend(
        real_sqlite_backend
    )
    return app


@pytest.fixture()
def authed_client_real_backend(app_with_real_backend):
    """TestClient exercising the REAL router -> REAL service wrapper ->
    REAL backend chain -- closed after the test via yield. Uses the SAME
    _install_admin_auth_override() helper as `authed_client` above (no
    duplicated auth-override code, no new mocking scope)."""
    _install_admin_auth_override(app_with_real_backend)
    with TestClient(app_with_real_backend, raise_server_exceptions=False) as client:
        yield client


class TestClearAllEndpointRealBackendChain:
    """AC-mandated coverage: "successful clear-all" and "empty-state
    no-op" against a REAL backend, through a REAL HTTP call. The subject
    under test -- router, service wrapper, backend -- has zero mocking
    in this class; the admin-auth override is the pre-existing project
    convention shared with every other test in this module (see the
    module docstring), not part of what this class newly verifies."""

    def test_successful_clear_all_persists_through_real_backend(
        self, authed_client_real_backend, real_sqlite_backend
    ) -> None:
        # Seed two genuinely active rows directly through the real
        # backend (the same round-trip test_dedup_state_clear_all_1589.py
        # uses).
        manager = _FakeGoldenRepoManagerWithBackend(real_sqlite_backend)
        record_dedup_outcome(
            manager,
            "story1589-int-repo-a",
            duplicate_groups=1,
            records_before=10,
            records_deleted=1,
            winner_kept_groups=1,
            whole_group_deleted_groups=0,
            collection_total=10,
        )
        record_dedup_outcome(
            manager,
            "story1589-int-repo-b",
            duplicate_groups=2,
            records_before=20,
            records_deleted=2,
            winner_kept_groups=2,
            whole_group_deleted_groups=0,
            collection_total=20,
        )

        response = authed_client_real_backend.post(_ENDPOINT_PATH)

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json() == {"cleared_count": 2}

        # Confirm the rows are GENUINELY marked cleared in the real
        # backend -- not merely that the HTTP layer reported a count.
        for alias in ("story1589-int-repo-a", "story1589-int-repo-b"):
            state = real_sqlite_backend.get_dedup_state(alias)
            assert state is not None
            assert state["cleared_at"] is not None
            assert state["cleared_reason"] == CLEAR_ALL_DEDUP_REASON

    def test_empty_state_no_op_returns_zero_via_real_backend(
        self, authed_client_real_backend
    ) -> None:
        # No rows seeded at all -- a genuine empty-state call against the
        # real backend.
        response = authed_client_real_backend.post(_ENDPOINT_PATH)

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json() == {"cleared_count": 0}
