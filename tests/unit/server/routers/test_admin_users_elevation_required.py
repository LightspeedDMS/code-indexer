"""
Tests for Story #923 AC6: admin user management endpoints require elevation.

Verifies:
- POST /api/admin/users returns 403 elevation_required without elevation window
- PUT /api/admin/users/{username} returns 403 elevation_required without elevation window
- DELETE /api/admin/users/{username} returns 403 elevation_required without elevation window
- PUT /api/admin/users/{username}/change-password returns 403 elevation_required without elevation window
- POST /api/admin/users returns 201 when elevation window is active (representative success path)
"""

import contextlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole

# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_GET_TOTP_SERVICE_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_KEY = "test-session-jti-admin-users"
_USERNAME = "testadmin"
_IP = "127.0.0.1"
_IDLE_TIMEOUT = 300
_MAX_AGE = 1800


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    return User(
        username=_USERNAME,
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def manager():
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_TIMEOUT,
        max_age_seconds=_MAX_AGE,
    )


@pytest.fixture(autouse=True)
def _restore_manager():
    """Restore module-level elevated_session_manager after each test."""
    original = getattr(_deps, "elevated_session_manager", None)
    yield
    _deps.elevated_session_manager = original


@contextlib.contextmanager
def _elevation_ctx(enforcement: bool = True, mfa_enabled: bool = True):
    """Patch kill-switch and TOTP service together."""
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_GET_TOTP_SERVICE_PATH, return_value=fake_totp_service),
    ):
        yield


@pytest.fixture
def client_no_elevation(admin_user, manager):
    """TestClient with admin auth but NO elevation window; enforcement ON."""
    _deps.elevated_session_manager = manager
    app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    yield TestClient(app, raise_server_exceptions=False), manager
    app.dependency_overrides.pop(_deps.get_current_admin_user_hybrid, None)


@pytest.fixture
def client_with_elevation(admin_user, manager):
    """TestClient with admin auth AND an active elevation window."""
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USERNAME, _IP)
    app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    yield TestClient(app, raise_server_exceptions=False), manager
    app.dependency_overrides.pop(_deps.get_current_admin_user_hybrid, None)


def _cookies():
    """Return session cookie dict matching the elevation window key."""
    return {"cidx_session": _SESSION_KEY}


# ---------------------------------------------------------------------------
# 403 elevation_required — parametrized across all 4 endpoints
# ---------------------------------------------------------------------------

_ENDPOINTS = [
    pytest.param(
        "POST",
        "/api/admin/users",
        {"username": "newuser", "password": "P@ss1234!", "role": "normal_user"},
        id="create_user",
    ),
    pytest.param(
        "PUT",
        "/api/admin/users/someuser",
        {"role": "normal_user"},
        id="update_user",
    ),
    pytest.param(
        "DELETE",
        "/api/admin/users/someuser",
        None,
        id="delete_user",
    ),
    pytest.param(
        "PUT",
        "/api/admin/users/someuser/change-password",
        {"new_password": "NewPass1!"},
        id="change_user_password",
    ),
]


@pytest.mark.parametrize("method,url,body", _ENDPOINTS)
def test_endpoint_without_elevation_returns_403(client_no_elevation, method, url, body):
    """All 4 admin user endpoints return 403 elevation_required without an elevation window."""
    client, _ = client_no_elevation
    with _elevation_ctx(enforcement=True):
        if method == "POST":
            resp = client.post(url, json=body)
        elif method == "PUT":
            resp = client.put(url, json=body)
        elif method == "DELETE":
            resp = client.delete(url)
        else:
            raise ValueError(f"Unexpected method: {method}")
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "elevation_required"


# ---------------------------------------------------------------------------
# Success with elevation — POST /api/admin/users (representative happy path)
# ---------------------------------------------------------------------------


def test_create_user_with_elevation_returns_201(client_with_elevation):
    """POST /api/admin/users returns 201 when an active elevation window exists."""
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _find_route_handler,
        _patch_closure,
    )

    client, _ = client_with_elevation
    handler = _find_route_handler("/api/admin/users", "POST")
    mock_um = Mock()
    mock_um.create_user.return_value = User(
        username="newuser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    with _elevation_ctx(enforcement=True):
        with _patch_closure(handler, "user_manager", mock_um):
            resp = client.post(
                "/api/admin/users",
                json={
                    "username": "newuser",
                    "password": "P@ss1234!",
                    "role": "normal_user",
                },
                cookies=_cookies(),
            )

    assert resp.status_code == 201, resp.text
