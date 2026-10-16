"""MFA setup pages render request- and account-derived values as text.

Drives the real routes through TestClient with an admin session (the session
lookup is patched, as in the other mfa route tests) and a real
ElevatedSessionManager.
"""

import contextlib
import html
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import CIDX_SESSION_COOKIE
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.web.mfa_routes import (
    mfa_router,
    set_totp_service,
    user_mfa_router,
)

_SESSION_USERNAME_PATH = "code_indexer.server.web.mfa_routes._get_session_username"
_ANY_SESSION_USERNAME_PATH = (
    "code_indexer.server.web.mfa_routes._get_any_session_username"
)
_ESM_PATH = "code_indexer.server.web.mfa_routes.elevated_session_manager"

_ADMIN = "admin_user"
# Every HTML-significant character plus a markup payload.
_HOSTILE = "eve\"'<>&<script>alert(1)</script><img src=x onerror=alert(2)>"
_ESCAPED = html.escape(_HOSTILE, quote=True)
_RAW_MARKUP = ("<script>alert(1)</script>", "<img src=x onerror=alert(2)>")


@pytest.fixture
def totp_mock():
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    svc.generate_secret.return_value = "JBSWY3DPEHPK3PXP"
    svc.regenerate_secret_cross_user.return_value = "JBSWY3DPEHPK3PXP"
    svc.get_provisioning_uri.return_value = "otpauth://totp/example"
    svc.generate_qr_code.return_value = b"\x89PNG\r\n"
    svc.get_manual_entry_key.return_value = "ABCD EFGH"
    svc.verify_code.return_value = True
    return svc


@pytest.fixture
def esm(tmp_path):
    return ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elev.db"),
    )


@pytest.fixture
def client(totp_mock, monkeypatch):
    from types import SimpleNamespace

    from code_indexer.server.web import auth as web_auth
    from code_indexer.server.web.auth import SessionManager

    monkeypatch.setattr(
        web_auth,
        "_session_manager",
        SessionManager("example-signing-key", SimpleNamespace(host="127.0.0.1")),
    )
    app = FastAPI()
    set_totp_service(totp_mock)
    app.include_router(mfa_router)
    app.include_router(user_mfa_router, prefix="/user/mfa")
    yield TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    set_totp_service(None)


@contextlib.contextmanager
def _elevated_admin(client: TestClient, esm: ElevatedSessionManager):
    session_key = f"test-session-{uuid.uuid4().hex}"
    esm.create(
        session_key=session_key,
        username=_ADMIN,
        elevated_from_ip=None,
        scope="full",
    )
    client.cookies.set(CIDX_SESSION_COOKIE, session_key)
    with patch(_SESSION_USERNAME_PATH, return_value=_ADMIN), patch(_ESM_PATH, esm):
        yield


def _assert_no_raw_markup(text: str) -> None:
    for fragment in _RAW_MARKUP:
        assert fragment not in text, f"raw markup rendered: {fragment!r}"


def test_show_mode_renders_target_user_as_text_in_body_and_attribute(client, esm):
    with _elevated_admin(client, esm):
        resp = client.get(
            "/admin/mfa/setup",
            params={"user": _HOSTILE, "mode": "show"},
        )
    assert resp.status_code == 200, resp.text
    _assert_no_raw_markup(resp.text)
    assert f"MFA for: <strong>{_ESCAPED}</strong>" in resp.text
    assert f"name='target_user' value='{_ESCAPED}'" in resp.text


def test_setup_mode_renders_target_user_as_text_in_attribute(client, esm):
    with _elevated_admin(client, esm):
        resp = client.get(
            "/admin/mfa/setup",
            params={"user": _HOSTILE, "confirm_overwrite": "1"},
        )
    assert resp.status_code == 200, resp.text
    _assert_no_raw_markup(resp.text)
    assert f"name='target_user' value='{_ESCAPED}'" in resp.text


def test_admin_not_configured_404_renders_target_user_as_text(client, esm, totp_mock):
    totp_mock.get_provisioning_uri.return_value = None
    with _elevated_admin(client, esm):
        resp = client.get(
            "/admin/mfa/setup",
            params={"user": _HOSTILE, "mode": "show"},
        )
    assert resp.status_code == 404
    _assert_no_raw_markup(resp.text)
    assert resp.text == f"No MFA configured for {_ESCAPED}"


def test_user_not_configured_404_renders_username_as_text(client, totp_mock):
    totp_mock.get_provisioning_uri.return_value = None
    with patch(_ANY_SESSION_USERNAME_PATH, return_value=_HOSTILE):
        resp = client.get("/user/mfa/setup")
    assert resp.status_code == 404
    _assert_no_raw_markup(resp.text)
    assert resp.text == f"No MFA configured for {_ESCAPED}"


def test_test_code_redirect_keeps_target_user_one_query_value(client):
    target = "eve&mode=new&user=admin_user"
    with patch(_SESSION_USERNAME_PATH, return_value=_ADMIN):
        resp = client.post(
            "/admin/mfa/verify",
            data={"totp_code": "123456", "target_user": target, "test_only": "1"},
        )
    assert resp.status_code == 303
    query = parse_qs(urlsplit(resp.headers["location"]).query)
    assert query["user"] == [target]
    assert query["mode"] == ["show"]
