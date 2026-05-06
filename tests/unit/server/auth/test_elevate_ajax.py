"""
TestClient-based unit tests for POST /auth/elevate-ajax (inline modal elevation).

The endpoint returns JSON (never redirects), enabling the in-page TOTP modal
introduced in Bug #955 UX improvement. All external dependencies are patched
with MagicMock — no real DB, no live server.
"""

import contextlib
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.web.elevation_web_routes import router as elevation_web_router

# ---------------------------------------------------------------------------
# Patch targets — must match the module where the names are looked up
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.web.elevation_web_routes._is_elevation_enforcement_enabled"
)
_TOTP_SERVICE_PATH = "code_indexer.server.web.elevation_web_routes.get_totp_service"
_ESM_PATH = "code_indexer.server.web.elevation_web_routes.elevated_session_manager"
_RESOLVE_SESSION_PATH = (
    "code_indexer.server.web.elevation_web_routes._resolve_session_key"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_JTI = "test-jti-ajax-xyz"
_USERNAME = "admin"
# Dummy bcrypt-format string — not a real credential; avoids hash computation.
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    """Minimal admin User with a dummy (non-functional) password hash."""
    from code_indexer.server.auth.user_manager import User

    return User(
        username=_USERNAME,
        role="admin",
        password_hash=_DUMMY_HASH,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def app(admin_user):
    """Minimal FastAPI app with only the elevation web router; admin auth overridden."""
    _app = FastAPI()
    _app.include_router(elevation_web_router)
    from code_indexer.server.auth import dependencies as _deps

    _app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    return _app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_totp(
    mfa_enabled: bool = True,
    totp_ok: bool = True,
    recovery_ok: bool = True,
) -> MagicMock:
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = mfa_enabled
    svc.verify_enabled_code.return_value = totp_ok
    svc.verify_recovery_code.return_value = recovery_ok
    return svc


def _fake_esm() -> MagicMock:
    esm = MagicMock()
    esm.create.return_value = None
    return esm


@contextlib.contextmanager
def _ajax_ctx(
    enforcement: bool = True,
    totp_svc=None,
    esm=None,
    session_key: Optional[str] = _SESSION_JTI,
):
    """Patch all four dependencies for elevate-ajax tests and yield the esm mock."""
    if totp_svc is None:
        totp_svc = _fake_totp()
    if esm is None:
        esm = _fake_esm()
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_TOTP_SERVICE_PATH, return_value=totp_svc),
        patch(_ESM_PATH, esm),
        patch(_RESOLVE_SESSION_PATH, return_value=session_key),
    ):
        yield esm


def _post_ajax(client, *, totp_code=None, recovery_code=None, cookie=None):
    """POST form data to /auth/elevate-ajax with an optional session cookie."""
    data = {}
    if totp_code is not None:
        data["totp_code"] = totp_code
    if recovery_code is not None:
        data["recovery_code"] = recovery_code
    cookies = {"cidx_session": cookie} if cookie else {}
    return client.post("/auth/elevate-ajax", data=data, cookies=cookies)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_success_returns_json_success_true(client):
    """Valid TOTP code with active session → 200 {"success": true}."""
    totp_svc = _fake_totp(mfa_enabled=True, totp_ok=True)
    esm = _fake_esm()
    with _ajax_ctx(totp_svc=totp_svc, esm=esm):
        resp = _post_ajax(client, totp_code="123456", cookie=_SESSION_JTI)

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    esm.create.assert_called_once()


def test_invalid_code_returns_json_success_false_401(client):
    """Wrong TOTP code → 401 {"success": false, "error": "Invalid code."}."""
    totp_svc = _fake_totp(mfa_enabled=True, totp_ok=False)
    esm = _fake_esm()
    with _ajax_ctx(totp_svc=totp_svc, esm=esm):
        resp = _post_ajax(client, totp_code="000000", cookie=_SESSION_JTI)

    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "Invalid code."
    esm.create.assert_not_called()


def test_no_code_provided_returns_400(client):
    """Neither totp_code nor recovery_code → 400 {"success": false}."""
    with _ajax_ctx():
        resp = _post_ajax(client, cookie=_SESSION_JTI)

    assert resp.status_code == 400
    assert resp.json()["success"] is False


def test_kill_switch_off_returns_503(client):
    """Elevation enforcement disabled → 503 {"success": false}."""
    esm = _fake_esm()
    with _ajax_ctx(enforcement=False, esm=esm):
        resp = _post_ajax(client, totp_code="123456", cookie=_SESSION_JTI)

    assert resp.status_code == 503
    assert resp.json()["success"] is False
    esm.create.assert_not_called()


def test_no_session_returns_403(client):
    """Session key resolves to None → 403 {"success": false, "error": "No session."}."""
    totp_svc = _fake_totp(mfa_enabled=True)
    esm = _fake_esm()
    with _ajax_ctx(totp_svc=totp_svc, esm=esm, session_key=None):
        resp = _post_ajax(client, totp_code="123456")

    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "No session."
    esm.create.assert_not_called()


def test_no_mfa_configured_returns_400(client):
    """TOTP not enabled for user → 400 {"success": false}."""
    totp_svc = _fake_totp(mfa_enabled=False)
    esm = _fake_esm()
    with _ajax_ctx(totp_svc=totp_svc, esm=esm):
        resp = _post_ajax(client, totp_code="123456", cookie=_SESSION_JTI)

    assert resp.status_code == 400
    assert resp.json()["success"] is False
    esm.create.assert_not_called()


def test_recovery_code_success(client):
    """Valid recovery code → 200 {"success": true}, elevation session created."""
    totp_svc = _fake_totp(mfa_enabled=True, recovery_ok=True)
    esm = _fake_esm()
    with _ajax_ctx(totp_svc=totp_svc, esm=esm):
        resp = _post_ajax(
            client, recovery_code="AAAA-BBBB-CCCC-DDDD", cookie=_SESSION_JTI
        )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    esm.create.assert_called_once()
