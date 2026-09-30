"""
Replacing an ACTIVE TOTP enrollment requires proof of the current factor.

Invariant: when elevation enforcement is ON and the account already has MFA
enabled, generating a new TOTP secret for it (self-service
`/user/mfa/setup?mode=new`, or an admin's own `/admin/mfa/setup`) requires
an elevation window owned by that same user. The window is opened on the
elevation page with a current TOTP code (scope "full") or a recovery code
(scope "totp_repair"); either is accepted. Without it, no secret is
generated and the caller is sent to the elevation page, returning to the
same setup URL afterwards.

Unchanged:
- first-time enrollment (no MFA enabled yet);
- the admin cross-user reset (admin elevation + confirm_overwrite=1);
- mode=show (read-only QR display);
- everything when elevation enforcement is OFF.

Front door: the real `mfa_router` / `user_mfa_router` mounted on a FastAPI
app, a real TOTPService on an isolated SQLite file, a real signed web
session cookie, and a real ElevatedSessionManager on an isolated file.
Only the enforcement switch is patched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Iterator
from unittest.mock import patch
from urllib.parse import quote

import pyotp
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from code_indexer.server.web.auth import SESSION_COOKIE_NAME, SessionManager

_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_USER = "reenroll-user"
_ADMIN = "reenroll-admin"
_OTHER_ADMIN = "reenroll-other-admin"
_ELEVATE_PAGE = "/admin/elevate"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def totp_service(tmp_path) -> Iterator[TOTPService]:
    svc = TOTPService(db_path=str(tmp_path / "mfa.db"))
    mfa_routes.set_totp_service(svc)
    yield svc
    mfa_routes.set_totp_service(None)


@pytest.fixture
def session_manager(monkeypatch) -> SessionManager:
    sm = SessionManager("reenroll-test-signing-key", SimpleNamespace(host="127.0.0.1"))
    monkeypatch.setattr(web_auth, "_session_manager", sm)
    return sm


@pytest.fixture
def esm(tmp_path, monkeypatch) -> ElevatedSessionManager:
    manager = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevated.db"),
    )
    monkeypatch.setattr(mfa_routes, "elevated_session_manager", manager)
    return manager


@pytest.fixture
def client(totp_service, session_manager, esm) -> TestClient:
    app = FastAPI()
    app.include_router(mfa_routes.mfa_router)
    app.include_router(mfa_routes.user_mfa_router, prefix="/user/mfa")
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_cookie(sm: SessionManager, username: str, role: str) -> str:
    resp = Response()
    sm.create_session(resp, username=username, role=role)
    header = resp.headers["set-cookie"]
    prefix = f"{SESSION_COOKIE_NAME}="
    assert header.startswith(prefix), header
    return header[len(prefix) :].split(";", 1)[0]


def _enroll(svc: TOTPService, username: str) -> str:
    """Fully enroll `username`; return the active secret."""
    secret = svc.generate_secret(username)
    assert svc.activate_mfa(username, pyotp.TOTP(secret).now())
    assert svc.is_mfa_enabled(username)
    return secret


def _active_key(svc: TOTPService, username: str) -> str:
    key = svc.get_manual_entry_key(username)
    assert key is not None
    return key


def _elevate(esm: ElevatedSessionManager, cookie: str, username: str, scope: str):
    esm.create(
        session_key=cookie, username=username, elevated_from_ip=None, scope=scope
    )


def _assert_sent_to_elevation(resp, return_to: str) -> None:
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == (
        f"{_ELEVATE_PAGE}?next={quote(return_to, safe='')}"
    )


# ---------------------------------------------------------------------------
# Self-service: /user/mfa/setup?mode=new
# ---------------------------------------------------------------------------


class TestUserReenrollEnforcementOn:
    def test_without_window_no_secret_is_generated(
        self, client, totp_service, session_manager
    ):
        _enroll(totp_service, _USER)
        before = _active_key(totp_service, _USER)
        cookie = _session_cookie(session_manager, _USER, "normal_user")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get(
                "/user/mfa/setup?mode=new", cookies={SESSION_COOKIE_NAME: cookie}
            )

        _assert_sent_to_elevation(resp, "/user/mfa/setup?mode=new")
        assert _active_key(totp_service, _USER) == before
        assert totp_service.is_mfa_enabled(_USER)

    def test_stolen_session_cannot_replace_authenticator(
        self, client, totp_service, session_manager
    ):
        """setup?mode=new then verify with a code from a new device must
        leave the owner's enrollment active and unchanged."""
        owner_secret = _enroll(totp_service, _USER)
        cookie = _session_cookie(session_manager, _USER, "normal_user")
        foreign_code = pyotp.TOTP(pyotp.random_base32()).now()

        with patch(_ENFORCEMENT_PATH, return_value=True):
            client.get(
                "/user/mfa/setup?mode=new", cookies={SESSION_COOKIE_NAME: cookie}
            )
            client.post(
                "/user/mfa/verify",
                data={"totp_code": foreign_code},
                cookies={SESSION_COOKIE_NAME: cookie},
            )

        assert totp_service.is_mfa_enabled(_USER)
        assert _active_key(totp_service, _USER).replace(" ", "") == owner_secret

    @pytest.mark.parametrize("scope", ["full", "totp_repair"])
    def test_with_own_window_new_secret_is_generated(
        self, client, totp_service, session_manager, esm, scope
    ):
        _enroll(totp_service, _USER)
        before = _active_key(totp_service, _USER)
        cookie = _session_cookie(session_manager, _USER, "normal_user")
        _elevate(esm, cookie, _USER, scope)

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get(
                "/user/mfa/setup?mode=new", cookies={SESSION_COOKIE_NAME: cookie}
            )

        assert resp.status_code == 200, resp.text
        assert "Verify and Activate MFA" in resp.text
        assert _active_key(totp_service, _USER) != before

    def test_window_owned_by_another_user_is_not_accepted(
        self, client, totp_service, session_manager, esm
    ):
        _enroll(totp_service, _USER)
        before = _active_key(totp_service, _USER)
        cookie = _session_cookie(session_manager, _USER, "normal_user")
        _elevate(esm, cookie, "someone-else", "full")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get(
                "/user/mfa/setup?mode=new", cookies={SESSION_COOKIE_NAME: cookie}
            )

        _assert_sent_to_elevation(resp, "/user/mfa/setup?mode=new")
        assert _active_key(totp_service, _USER) == before

    @pytest.mark.parametrize("path", ["/user/mfa/setup", "/user/mfa/setup?mode=new"])
    def test_first_enrollment_without_mfa_is_unchanged(
        self, client, totp_service, session_manager, path
    ):
        cookie = _session_cookie(session_manager, _USER, "normal_user")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            setup = client.get(path, cookies={SESSION_COOKIE_NAME: cookie})
            assert setup.status_code == 200, setup.text
            secret = _active_key(totp_service, _USER).replace(" ", "")
            verify = client.post(
                "/user/mfa/verify",
                data={"totp_code": pyotp.TOTP(secret).now()},
                cookies={SESSION_COOKIE_NAME: cookie},
            )

        assert verify.status_code == 200, verify.text
        assert "MFA Activated Successfully" in verify.text
        assert totp_service.is_mfa_enabled(_USER)

    def test_show_mode_is_unchanged(self, client, totp_service, session_manager):
        _enroll(totp_service, _USER)
        before = _active_key(totp_service, _USER)
        cookie = _session_cookie(session_manager, _USER, "normal_user")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get("/user/mfa/setup", cookies={SESSION_COOKIE_NAME: cookie})

        assert resp.status_code == 200, resp.text
        assert "Two-Factor Authentication (Active)" in resp.text
        assert _active_key(totp_service, _USER) == before


class TestUserReenrollEnforcementOff:
    def test_without_window_reenroll_proceeds_as_before(
        self, client, totp_service, session_manager
    ):
        _enroll(totp_service, _USER)
        before = _active_key(totp_service, _USER)
        cookie = _session_cookie(session_manager, _USER, "normal_user")

        with patch(_ENFORCEMENT_PATH, return_value=False):
            resp = client.get(
                "/user/mfa/setup?mode=new", cookies={SESSION_COOKIE_NAME: cookie}
            )

        assert resp.status_code == 200, resp.text
        assert _active_key(totp_service, _USER) != before


# ---------------------------------------------------------------------------
# Admin: /admin/mfa/setup for the admin's OWN account, and cross-user reset
# ---------------------------------------------------------------------------


class TestAdminReenroll:
    def test_own_reenroll_without_window_is_refused(
        self, client, totp_service, session_manager
    ):
        _enroll(totp_service, _ADMIN)
        before = _active_key(totp_service, _ADMIN)
        cookie = _session_cookie(session_manager, _ADMIN, "admin")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get("/admin/mfa/setup", cookies={SESSION_COOKIE_NAME: cookie})

        _assert_sent_to_elevation(resp, "/admin/mfa/setup")
        assert _active_key(totp_service, _ADMIN) == before
        assert totp_service.is_mfa_enabled(_ADMIN)

    def test_own_reenroll_via_explicit_user_param_without_window_is_refused(
        self, client, totp_service, session_manager
    ):
        _enroll(totp_service, _ADMIN)
        before = _active_key(totp_service, _ADMIN)
        cookie = _session_cookie(session_manager, _ADMIN, "admin")
        path = f"/admin/mfa/setup?user={_ADMIN}&confirm_overwrite=1"

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get(path, cookies={SESSION_COOKIE_NAME: cookie})

        _assert_sent_to_elevation(resp, path)
        assert _active_key(totp_service, _ADMIN) == before

    def test_own_reenroll_with_window_proceeds(
        self, client, totp_service, session_manager, esm
    ):
        _enroll(totp_service, _ADMIN)
        before = _active_key(totp_service, _ADMIN)
        cookie = _session_cookie(session_manager, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "full")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get("/admin/mfa/setup", cookies={SESSION_COOKIE_NAME: cookie})

        assert resp.status_code == 200, resp.text
        assert _active_key(totp_service, _ADMIN) != before

    def test_own_first_enrollment_is_unchanged(
        self, client, totp_service, session_manager
    ):
        cookie = _session_cookie(session_manager, _ADMIN, "admin")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get("/admin/mfa/setup", cookies={SESSION_COOKIE_NAME: cookie})

        assert resp.status_code == 200, resp.text
        assert "Verify and Activate MFA" in resp.text

    def test_own_reenroll_enforcement_off_proceeds_as_before(
        self, client, totp_service, session_manager
    ):
        _enroll(totp_service, _ADMIN)
        before = _active_key(totp_service, _ADMIN)
        cookie = _session_cookie(session_manager, _ADMIN, "admin")

        with patch(_ENFORCEMENT_PATH, return_value=False):
            resp = client.get("/admin/mfa/setup", cookies={SESSION_COOKIE_NAME: cookie})

        assert resp.status_code == 200, resp.text
        assert _active_key(totp_service, _ADMIN) != before

    def test_cross_user_reset_keeps_its_existing_rules(
        self, client, totp_service, session_manager, esm
    ):
        """Admin elevation + confirm_overwrite=1 is still sufficient to reset
        ANOTHER user's active MFA; the target's own factor is not required."""
        _enroll(totp_service, _OTHER_ADMIN)
        before = _active_key(totp_service, _OTHER_ADMIN)
        cookie = _session_cookie(session_manager, _ADMIN, "admin")
        _elevate(esm, cookie, _ADMIN, "full")

        with patch(_ENFORCEMENT_PATH, return_value=True):
            resp = client.get(
                f"/admin/mfa/setup?user={_OTHER_ADMIN}&confirm_overwrite=1",
                cookies={SESSION_COOKIE_NAME: cookie},
            )

        assert resp.status_code == 200, resp.text
        assert _active_key(totp_service, _OTHER_ADMIN) != before
