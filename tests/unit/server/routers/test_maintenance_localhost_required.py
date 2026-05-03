"""Tests for maintenance router localhost-only enforcement (Story #924 AC2).

Uses TestClient(app, client=(host, port)) to control request.client.host so the
real require_localhost dependency is exercised -- not mocked.

Verifies that:
- POST /enter from 127.0.0.1 passes (admin auth still required/satisfied)
- POST /enter from external IP returns 403
- POST /exit from 127.0.0.1 passes
- POST /exit from external IP returns 403
- GET /status from external IP returns 200 (require_localhost not applied)
"""

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import get_current_admin_user


_LOCALHOST = ("127.0.0.1", 50000)
_EXTERNAL = ("10.0.0.1", 50000)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_and_clean():
    """Reset maintenance state and dependency overrides around each test."""
    from code_indexer.server.services.maintenance_service import (
        _reset_maintenance_state,
    )

    _reset_maintenance_state()
    yield
    from code_indexer.server.app import app

    app.dependency_overrides.clear()


@pytest.fixture
def admin_user():
    return User(
        username="admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _make_client(peer, admin_user):
    """Build TestClient with real require_localhost active.

    peer: tuple (host, port) -- controls request.client visible to dependencies.
    Only get_current_admin_user is bypassed so the test focuses on localhost gating.
    """
    from code_indexer.server.app import app

    app.dependency_overrides[get_current_admin_user] = lambda: admin_user
    return TestClient(app, client=peer)


# ---------------------------------------------------------------------------
# POST /enter
# ---------------------------------------------------------------------------


class TestEnterMaintenanceLocalhostRequired:
    def test_enter_from_localhost_passes(self, admin_user):
        """POST /enter from 127.0.0.1 with admin auth must succeed (200)."""
        client = _make_client(_LOCALHOST, admin_user)
        response = client.post("/api/admin/maintenance/enter")
        assert response.status_code == 200

    def test_enter_from_external_returns_403(self, admin_user):
        """POST /enter from non-loopback must be rejected with 403."""
        client = _make_client(_EXTERNAL, admin_user)
        response = client.post("/api/admin/maintenance/enter")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# POST /exit
# ---------------------------------------------------------------------------


class TestExitMaintenanceLocalhostRequired:
    def test_exit_from_localhost_passes(self, admin_user):
        """POST /exit from 127.0.0.1 with admin auth must succeed (200)."""
        client = _make_client(_LOCALHOST, admin_user)
        response = client.post("/api/admin/maintenance/exit")
        assert response.status_code == 200

    def test_exit_from_external_returns_403(self, admin_user):
        """POST /exit from non-loopback must be rejected with 403."""
        client = _make_client(_EXTERNAL, admin_user)
        response = client.post("/api/admin/maintenance/exit")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# GET /status (read-only -- must remain accessible from any origin)
# ---------------------------------------------------------------------------


class TestStatusUnaffected:
    def test_status_unaffected_from_external(self, admin_user):
        """GET /status must be reachable from non-loopback (no localhost gate)."""
        client = _make_client(_EXTERNAL, admin_user)
        response = client.get("/api/admin/maintenance/status")
        assert response.status_code == 200
