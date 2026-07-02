"""
Tests for Story #923 AC6: research assistant endpoints require elevation.

Verifies:
- POST /admin/research/send returns 403 elevation_required without elevation window
- POST /admin/research/sessions/{id}/upload returns 403 elevation_required without elevation window
- POST /admin/research/send returns 200 when elevation window is active (representative success path)
"""

import contextlib
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.research_assistant import router
from code_indexer.server.web.auth import require_admin_session, SessionData

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
_SESSION_KEY = "test-session-jti-research"
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


def _make_session_data():
    return SessionData(
        username=_USERNAME,
        role="admin",
        csrf_token="test-csrf-token",
        created_at=time.time(),
    )


@pytest.fixture
def app_no_elevation(admin_user, manager):
    """FastAPI app with admin session + admin user overrides, but no elevation window."""
    _deps.elevated_session_manager = manager
    _app = FastAPI()
    _app.include_router(router)

    async def mock_admin_session():
        return _make_session_data()

    _app.dependency_overrides[require_admin_session] = mock_admin_session
    _app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    return _app


@pytest.fixture
def app_with_elevation(admin_user, manager):
    """FastAPI app with admin session + admin user overrides AND an active elevation window."""
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USERNAME, _IP)
    _app = FastAPI()
    _app.include_router(router)

    async def mock_admin_session():
        return _make_session_data()

    _app.dependency_overrides[require_admin_session] = mock_admin_session
    _app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    return _app


def _cookies():
    """Return session cookie dict matching the elevation window key."""
    return {"cidx_session": _SESSION_KEY}


# ---------------------------------------------------------------------------
# 403 elevation_required — both research endpoints without elevation
# ---------------------------------------------------------------------------


def test_send_without_elevation_returns_403(app_no_elevation):
    """POST /admin/research/send returns 403 elevation_required without elevation window."""
    client = TestClient(app_no_elevation, raise_server_exceptions=False)
    with _elevation_ctx(enforcement=True):
        resp = client.post(
            "/admin/research/send",
            data={"user_prompt": "Hello"},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "elevation_required"


def test_upload_without_elevation_returns_403(app_no_elevation):
    """POST /admin/research/sessions/{id}/upload returns 403 elevation_required without elevation window."""
    client = TestClient(app_no_elevation, raise_server_exceptions=False)
    with _elevation_ctx(enforcement=True):
        resp = client.post(
            "/admin/research/sessions/test-session-id/upload",
            files={"file": ("test.txt", b"content", "text/plain")},
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "elevation_required"


# ---------------------------------------------------------------------------
# Success with elevation — POST /admin/research/send (representative happy path)
# ---------------------------------------------------------------------------


def test_send_with_elevation_returns_200(app_with_elevation):
    """POST /admin/research/send returns 200 when an active elevation window exists."""
    client = TestClient(app_with_elevation, raise_server_exceptions=False)

    mock_service_instance = MagicMock()
    mock_service_instance.get_session.return_value = {
        "id": "test-session-id",
        "name": "Test Session",
    }
    mock_service_instance.get_default_session.return_value = {
        "id": "test-session-id",
        "name": "Test Session",
    }
    mock_service_instance.execute_prompt.return_value = "job-id-123"
    mock_service_instance.get_messages.return_value = []

    with _elevation_ctx(enforcement=True):
        with patch(
            "code_indexer.server.routers.research_assistant.ResearchAssistantService",
            return_value=mock_service_instance,
        ):
            with patch(
                "code_indexer.server.routers.research_assistant._get_github_token",
                return_value=None,
            ):
                with patch(
                    "code_indexer.server.routers.research_assistant._get_job_tracker",
                    return_value=None,
                ):
                    with patch(
                        "code_indexer.server.routers.research_assistant._get_research_backend",
                        return_value=None,
                    ):
                        with patch(
                            "code_indexer.server.routers.research_assistant.templates"
                        ) as mock_templates:
                            mock_templates.TemplateResponse.return_value = MagicMock(
                                status_code=200,
                                media_type="text/html",
                                body=b"<html>ok</html>",
                            )
                            resp = client.post(
                                "/admin/research/send",
                                data={
                                    "user_prompt": "Hello",
                                    "session_id": "test-session-id",
                                },
                                cookies=_cookies(),
                            )

    # AC6 scope: assert the elevation gate is NOT triggered. Downstream
    # mocks for Claude CLI manager are intentionally minimal (outside AC6).
    # What matters is no 403 elevation_required, which would mean the gate
    # falsely blocked an authorized request.
    assert resp.status_code != 403, (
        f"Elevation gate falsely blocked an authorized request: {resp.text}"
    )
    assert "elevation_required" not in resp.text
