"""
Tests for user (non-admin) MFA routes.

Verifies that non-admin users can set up, verify, view status,
and disable MFA via /user/mfa/* endpoints, and that these endpoints
never accept a target_user parameter.
"""

import os
import tempfile

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.web.mfa_routes import (
    set_totp_service,
    user_mfa_router,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeServerConfig:
    """Minimal server config for SessionManager."""

    host = "127.0.0.1"


class _FakeWebSecurityConfig:
    admin_session_timeout_seconds = 28800
    web_session_timeout_seconds = 28800


@pytest.fixture
def totp_service():
    """Real TOTPService with temp database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    key = Fernet.generate_key().decode()
    service = TOTPService(db_path=db_path, mfa_encryption_key=key)
    yield service
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def app_with_user_mfa(totp_service):
    """FastAPI app with user_mfa_router mounted at /user/mfa."""
    app = FastAPI()
    set_totp_service(totp_service)
    app.include_router(user_mfa_router, prefix="/user/mfa", tags=["user-mfa"])

    from code_indexer.server.web.auth import init_session_manager

    init_session_manager(
        "test-secret-key",
        _FakeServerConfig(),
        _FakeWebSecurityConfig(),
    )

    yield app
    set_totp_service(None)


@pytest.fixture
def client(app_with_user_mfa):
    """TestClient for the app."""
    return TestClient(app_with_user_mfa, follow_redirects=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _create_user_session_cookie(
    username: str = "regularuser", role: str = "user"
) -> str:
    """Create a valid signed session cookie for a non-admin user."""
    from code_indexer.server.web.auth import get_session_manager
    from fastapi.responses import Response

    session_mgr = get_session_manager()
    response = Response()
    session_mgr.create_session(response, username=username, role=role)
    for header_name, header_value in response.raw_headers:
        if header_name == b"set-cookie":
            cookie_str = header_value.decode()
            parts = cookie_str.split(";")
            key_val = parts[0]  # "session=VALUE"
            return key_val.split("=", 1)[1]
    raise RuntimeError("No session cookie found in response")


def test_user_mfa_setup_requires_authentication(client):
    """GET /user/mfa/setup without session redirects to /login."""
    resp = client.get("/user/mfa/setup")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_user_mfa_verify_requires_auth(client):
    """POST /user/mfa/verify without session redirects to /login."""
    resp = client.post("/user/mfa/verify", data={"totp_code": "123456"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_user_mfa_disable_requires_auth(client):
    """POST /user/mfa/disable without session redirects to /login."""
    resp = client.post("/user/mfa/disable", data={"totp_code": "123456"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_user_mfa_recovery_requires_auth(client):
    """GET /user/mfa/recovery-codes without session redirects to /login."""
    resp = client.get("/user/mfa/recovery-codes")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_user_mfa_setup_accessible_to_non_admin(client):
    """GET /user/mfa/setup with valid user session returns 200 with setup HTML."""
    cookie_val = _create_user_session_cookie("regularuser", "user")
    resp = client.get("/user/mfa/setup", cookies={"session": cookie_val})
    assert resp.status_code == 200
    assert "Set Up Two-Factor Authentication" in resp.text


def test_user_mfa_setup_form_action_points_to_user_verify(client):
    """Setup form must POST to /user/mfa/verify, not /admin/mfa/verify."""
    cookie_val = _create_user_session_cookie("regularuser", "user")
    resp = client.get("/user/mfa/setup", cookies={"session": cookie_val})
    assert "action='/user/mfa/verify'" in resp.text
    assert "/admin/mfa/verify" not in resp.text


def test_user_mfa_verify_activates_for_session_user(client, totp_service):
    """POST /user/mfa/verify with valid code activates MFA and shows recovery codes."""
    username = "regularuser"
    cookie_val = _create_user_session_cookie(username, "user")
    totp_service.generate_secret(username)
    secret = totp_service._get_secret(username)
    valid_code = pyotp.TOTP(secret).now()

    resp = client.post(
        "/user/mfa/verify",
        data={"totp_code": valid_code},
        cookies={"session": cookie_val},
    )
    assert resp.status_code == 200
    assert "MFA Activated Successfully" in resp.text
    assert "recovery codes" in resp.text.lower()
    assert "href='/user/api-keys'" in resp.text
    assert totp_service.is_mfa_enabled(username) is True


def test_user_mfa_status_returns_json(client):
    """GET /user/mfa/status returns JSON with mfa_enabled and username."""
    cookie_val = _create_user_session_cookie("regularuser", "user")
    resp = client.get("/user/mfa/status", cookies={"session": cookie_val})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mfa_enabled"] is False
    assert data["username"] == "regularuser"


def test_user_mfa_routes_do_not_accept_target_user(client, totp_service):
    """POST /user/mfa/verify ignores target_user — activates for session user only."""
    username = "regularuser"
    cookie_val = _create_user_session_cookie(username, "user")
    totp_service.generate_secret(username)
    secret = totp_service._get_secret(username)
    valid_code = pyotp.TOTP(secret).now()

    resp = client.post(
        "/user/mfa/verify",
        data={"totp_code": valid_code, "target_user": "admin"},
        cookies={"session": cookie_val},
    )
    assert resp.status_code == 200
    assert totp_service.is_mfa_enabled(username) is True
    assert totp_service.is_mfa_enabled("admin") is False


def test_user_mfa_disable_with_valid_code(client, totp_service):
    """POST /user/mfa/disable with valid recovery code disables MFA and redirects."""
    username = "regularuser"
    cookie_val = _create_user_session_cookie(username, "user")
    totp_service.generate_secret(username)
    secret = totp_service._get_secret(username)
    code = pyotp.TOTP(secret).now()
    totp_service.activate_mfa(username, code)
    assert totp_service.is_mfa_enabled(username) is True

    # Use recovery code since TOTP replay protection blocks reuse in same window
    recovery_codes = totp_service.generate_recovery_codes(username)
    resp = client.post(
        "/user/mfa/disable",
        data={"totp_code": recovery_codes[0]},
        cookies={"session": cookie_val},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/user/api-keys"
    assert totp_service.is_mfa_enabled(username) is False


def test_user_mfa_recovery_codes_page(client, totp_service):
    """GET /user/mfa/recovery-codes returns codes with done link to /user/api-keys."""
    username = "regularuser"
    cookie_val = _create_user_session_cookie(username, "user")
    totp_service.generate_secret(username)
    secret = totp_service._get_secret(username)
    code = pyotp.TOTP(secret).now()
    totp_service.activate_mfa(username, code)

    resp = client.get(
        "/user/mfa/recovery-codes",
        cookies={"session": cookie_val},
    )
    assert resp.status_code == 200
    assert "recovery codes" in resp.text.lower()
    assert "href='/user/api-keys'" in resp.text
