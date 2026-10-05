"""
Invariant: when a user has MFA (TOTP) configured, every login-issuing path
completes the MFA verification step before a session or an authorization
code is issued. A user who has not configured MFA is unaffected by this
invariant on any of these paths -- no new prompt, no forced enrollment.

Covers two front doors, all through the real FastAPI app (TestClient),
with a real UserManager and a real TOTPService (no mocking of the
component under test):

1. The Web unified-login endpoint's expired-password branch
   (POST /login -> redirect to the change-password page).
2. The OIDC callback's OAuth-authorization branch
   (GET /auth/sso/callback with flow=oauth_authorize -> issues an
   authorization code to an OAuth client). The OIDC provider's network
   calls (token exchange, userinfo) are mocked -- the same boundary this
   suite's other OIDC-callback tests already draw -- so only the local
   MFA-gating behaviour under test is exercised for real.
"""

import base64
import hashlib
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not found in login page HTML"
    return m.group(1)


def _extract_challenge_token(html: str) -> str:
    m = re.search(r"name='challenge_token' value='([^']+)'", html)
    assert m, "challenge_token not found in MFA challenge page"
    return m.group(1)


def _real_totp_service(tmpdir_path: str):
    from code_indexer.server.auth.totp_service import TOTPService

    return TOTPService(db_path=str(Path(tmpdir_path) / "mfa.db"))


def _totp_secret(totp_service, username: str) -> str:
    uri = totp_service.get_provisioning_uri(username)
    assert uri is not None
    return str(uri.split("secret=")[1].split("&")[0])


def _enroll_totp(totp_service, username: str) -> None:
    """Enroll and activate TOTP for a user using a real (t-1) code, leaving
    the current-window code available for a subsequent verification call."""
    import pyotp

    totp_service.generate_secret(username)
    totp = pyotp.TOTP(_totp_secret(totp_service, username))
    past_code = totp.at(int(time.time()) - 30)
    assert totp_service.activate_mfa(username, past_code) is True


def _current_totp_code(totp_service, username: str) -> str:
    import pyotp

    return pyotp.TOTP(_totp_secret(totp_service, username)).now()


# ---------------------------------------------------------------------------
# Part 1: Web login, expired-password branch
# ---------------------------------------------------------------------------


def _get_full_app(tmpdir: str):
    """A genuinely fresh app bound to an isolated data dir.

    Calls create_app() directly rather than importing the module-level
    `app` singleton: that singleton is constructed once per process and
    cached (server/app.py's lazy PEP 562 wiring), so a second test's temp
    dir would never take effect and every test after the first would keep
    talking to the first test's (by then deleted) directory.
    """
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    with patch.dict(
        "os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir, "CIDX_DATA_DIR": tmpdir}
    ):
        reset_config_service()
        return create_app()


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _create_expired_password_user(username: str, password: str):
    """Create a real user and force their password to read as expired."""
    from code_indexer.server.auth import dependencies
    from code_indexer.server.auth.user_manager import UserRole
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.utils.config_manager import PasswordExpiryConfig

    assert dependencies.user_manager is not None  # real app has wired this
    dependencies.user_manager.create_user(
        username=username, password=password, role=UserRole.NORMAL_USER
    )

    svc = get_config_service()
    cfg = svc.get_config()
    cfg.password_expiry_config = PasswordExpiryConfig(enabled=True, max_age_days=90)
    svc.save_config(cfg)

    old_time = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
    user_manager = dependencies.user_manager
    assert user_manager is not None  # real app has wired this
    sqlite_backend = user_manager._sqlite_backend
    assert sqlite_backend is not None  # real app wires the SQLite backend
    sqlite_backend.set_password_changed_at(username, old_time)


class TestPasswordExpiredLoginMfaGate:
    """POST /login, expired-password branch."""

    def test_totp_enrolled_user_gets_no_session_before_completing_totp(
        self, tmpdir_path
    ):
        """A password-expired login for a user with MFA configured must not
        grant a working session (nor access to another authenticated Web
        page) until the MFA step is completed."""
        from code_indexer.server.web import mfa_routes

        username, password = "expired-mfa-user-1", "Str0ng!Passw0rd#Xyz1"
        app = _get_full_app(tmpdir_path)
        _create_expired_password_user(username, password)
        totp_service = _real_totp_service(tmpdir_path)
        _enroll_totp(totp_service, username)
        mfa_routes.set_totp_service(totp_service)
        try:
            client = TestClient(app, follow_redirects=False)
            csrf = _extract_csrf(client.get("/login").text)

            client.post(
                "/login",
                data={
                    "username": username,
                    "password": password,
                    "csrf_token": csrf,
                },
            )

            assert "session" not in client.cookies, (
                "a working session must not exist before MFA is verified"
            )

            api_keys_resp = client.get("/user/api-keys")
            assert api_keys_resp.status_code != 200, (
                "an authenticated Web page must not be reachable before "
                f"MFA is verified; got {api_keys_resp.status_code}"
            )
        finally:
            mfa_routes.set_totp_service(None)

    def test_totp_enrolled_user_completes_totp_then_reaches_change_password(
        self, tmpdir_path
    ):
        """After completing the MFA step, the user lands on the
        change-password redirect with a working session -- the outcome
        the expired-password branch always intended, now gated on MFA."""
        from code_indexer.server.web import mfa_routes

        username, password = "expired-mfa-user-2", "Str0ng!Passw0rd#Xyz2"
        app = _get_full_app(tmpdir_path)
        _create_expired_password_user(username, password)
        totp_service = _real_totp_service(tmpdir_path)
        _enroll_totp(totp_service, username)
        mfa_routes.set_totp_service(totp_service)
        try:
            client = TestClient(app, follow_redirects=False)
            csrf = _extract_csrf(client.get("/login").text)

            challenge_page = client.post(
                "/login",
                data={
                    "username": username,
                    "password": password,
                    "csrf_token": csrf,
                },
            )
            challenge_token = _extract_challenge_token(challenge_page.text)
            totp_code = _current_totp_code(totp_service, username)

            verify_resp = client.post(
                "/admin/mfa/challenge/verify",
                data={"challenge_token": challenge_token, "totp_code": totp_code},
            )

            assert verify_resp.status_code == 303, verify_resp.text
            assert "change-password" in verify_resp.headers.get("location", "")
            assert "session" in client.cookies

            api_keys_resp = client.get("/user/api-keys")
            assert api_keys_resp.status_code == 200, api_keys_resp.text
        finally:
            mfa_routes.set_totp_service(None)

    def test_user_without_mfa_configured_is_unchanged(self, tmpdir_path):
        """A password-expired login for a user with NO MFA configured must
        behave exactly as before: an immediate session and redirect to the
        change-password page, with no MFA prompt of any kind."""
        from code_indexer.server.web import mfa_routes

        username, password = "expired-no-mfa-user", "Str0ng!Passw0rd#Xyz3"
        app = _get_full_app(tmpdir_path)
        _create_expired_password_user(username, password)
        mfa_routes.set_totp_service(None)
        try:
            client = TestClient(app, follow_redirects=False)
            csrf = _extract_csrf(client.get("/login").text)

            resp = client.post(
                "/login",
                data={
                    "username": username,
                    "password": password,
                    "csrf_token": csrf,
                },
            )

            assert resp.status_code == 303, resp.text
            assert "change-password" in resp.headers.get("location", "")
            assert "session" in client.cookies

            api_keys_resp = client.get("/user/api-keys")
            assert api_keys_resp.status_code == 200, api_keys_resp.text
        finally:
            mfa_routes.set_totp_service(None)


# ---------------------------------------------------------------------------
# Part 2: OIDC callback, OAuth-authorization branch
# ---------------------------------------------------------------------------


def _register_oauth_client_and_pkce(tmp: Path):
    from code_indexer.server.auth.oauth.oauth_manager import OAuthManager

    oauth_mgr = OAuthManager(
        db_path=str(tmp / "oauth.db"), issuer="http://localhost:8000"
    )
    client_info = oauth_mgr.register_client(
        client_name="OIDC MFA Test Client",
        redirect_uris=["https://example.com/callback"],
    )
    verifier = secrets.token_urlsafe(64)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return oauth_mgr, client_info, code_challenge, verifier


def _mocked_oidc_manager_with_user(username: str):
    """OIDCManager whose network-facing provider calls are mocked; only the
    local MFA-gating branch under test runs for real."""
    from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
    from code_indexer.server.auth.oidc.oidc_provider import OIDCProvider, OIDCUserInfo
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    config = OIDCProviderConfig(
        enabled=True, issuer_url="https://example.com", client_id="test-client-id"
    )
    oidc_mgr = OIDCManager(config, None, None)
    oidc_mgr.provider = Mock(spec=OIDCProvider)
    oidc_mgr.provider.exchange_code_for_token = AsyncMock(
        return_value={"access_token": "tok", "id_token": "id-tok"}
    )
    oidc_mgr.provider.get_user_info = Mock(
        return_value=OIDCUserInfo(
            subject="sso-sub-1", email="sso-oauth@example.com", email_verified=True
        )
    )
    test_user = User(
        username=username,
        role=UserRole.NORMAL_USER,
        password_hash="",
        created_at=datetime.now(timezone.utc),
        email="sso-oauth@example.com",
    )
    oidc_mgr.match_or_create_user = AsyncMock(  # type: ignore[method-assign]
        return_value=test_user
    )
    return oidc_mgr


def _build_oidc_oauth_app(tmp: Path):
    """Minimal app mounting the OIDC callback router and the OAuth router
    (the latter owns POST /oauth/mfa/verify, which the OIDC callback's
    MFA-gated branch must hand off to)."""
    from code_indexer.server.auth.oauth import routes as oauth_routes
    from code_indexer.server.auth.oidc.routes import router as oidc_router
    from code_indexer.server.auth.oidc import state_manager as state_manager_module
    from code_indexer.server.auth.oidc.state_manager import StateManager
    import code_indexer.server.auth.oidc.routes as oidc_routes_module

    username = "sso-oauth-user"
    oauth_mgr, client_info, code_challenge, verifier = _register_oauth_client_and_pkce(
        tmp
    )
    oidc_mgr = _mocked_oidc_manager_with_user(username)

    # Isolate from any SQLite path a previously-run test's StateManager
    # left configured on this process-wide module global.
    with (
        patch.object(state_manager_module, "_configured_sqlite_path", None),
        patch.dict("os.environ", {"CIDX_DATA_DIR": str(tmp)}),
    ):
        state_mgr = StateManager()
    state_token = state_mgr.create_state(
        {
            "flow": "oauth_authorize",
            "client_id": client_info["client_id"],
            "code_challenge": code_challenge,
            "redirect_uri": "https://example.com/callback",
            "oauth_state": "test_state",
            "oidc_code_verifier": verifier,
        }
    )

    oidc_routes_module.oidc_manager = oidc_mgr
    oidc_routes_module.state_manager = state_mgr

    app = FastAPI()
    app.include_router(oidc_router)
    app.include_router(oauth_routes.router)
    app.state.oauth_manager = oauth_mgr
    app.dependency_overrides[oauth_routes.get_oauth_manager] = lambda: oauth_mgr
    # The server's account store, holding the SSO-linked account.
    from code_indexer.server.auth.user_manager import UserManager, UserRole

    accounts = UserManager(users_file_path=str(tmp / "users.json"))
    accounts.create_user(username, "Str0ng!Passw0rd#Xyz1", UserRole.NORMAL_USER)
    app.state.user_manager = accounts

    return app, state_token, username


@pytest.fixture
def oidc_temp_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestOidcOauthAuthorizeMfaGate:
    """GET /auth/sso/callback, flow=oauth_authorize branch."""

    def test_totp_enrolled_user_gets_no_code_before_completing_totp(
        self, oidc_temp_dir
    ):
        from code_indexer.server.web import mfa_routes

        app, state_token, username = _build_oidc_oauth_app(oidc_temp_dir)
        totp_service = _real_totp_service(str(oidc_temp_dir))
        _enroll_totp(totp_service, username)
        mfa_routes.set_totp_service(totp_service)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                f"/auth/sso/callback?code=test-auth-code&state={state_token}",
                follow_redirects=False,
            )

            assert resp.status_code != 302, (
                "an authorization code must not be issued before MFA is "
                f"verified; got a redirect: {resp.headers.get('location')}"
            )
            assert "code=" not in resp.headers.get("location", "")
        finally:
            mfa_routes.set_totp_service(None)

    def test_totp_enrolled_user_completes_totp_then_gets_code(self, oidc_temp_dir):
        from code_indexer.server.web import mfa_routes

        app, state_token, username = _build_oidc_oauth_app(oidc_temp_dir)
        totp_service = _real_totp_service(str(oidc_temp_dir))
        _enroll_totp(totp_service, username)
        mfa_routes.set_totp_service(totp_service)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            challenge_resp = client.get(
                f"/auth/sso/callback?code=test-auth-code&state={state_token}",
                follow_redirects=False,
            )
            challenge_token = _extract_challenge_token(challenge_resp.text)
            totp_code = _current_totp_code(totp_service, username)

            verify_resp = client.post(
                "/oauth/mfa/verify",
                data={"challenge_token": challenge_token, "totp_code": totp_code},
                follow_redirects=False,
            )

            assert verify_resp.status_code == 302, verify_resp.text
            location = verify_resp.headers["location"]
            assert location.startswith("https://example.com/callback")
            assert "code=" in location
        finally:
            mfa_routes.set_totp_service(None)

    def test_user_without_mfa_configured_is_unchanged(self, oidc_temp_dir):
        from code_indexer.server.web import mfa_routes

        app, state_token, _username = _build_oidc_oauth_app(oidc_temp_dir)
        mfa_routes.set_totp_service(None)
        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                f"/auth/sso/callback?code=test-auth-code&state={state_token}",
                follow_redirects=False,
            )

            assert resp.status_code == 302, resp.text
            location = resp.headers["location"]
            assert location.startswith("https://example.com/callback")
            assert "code=" in location
        finally:
            mfa_routes.set_totp_service(None)
