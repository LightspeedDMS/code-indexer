"""
Tests for Bug #624: OAuth /authorize rejects valid PKCE requests missing optional `state`.

Per OAuth 2.1 with PKCE, `state` is OPTIONAL. Claude.ai's OAuth PKCE flow sends
`code_challenge` but no `state`, causing HTTP 422 (Unprocessable Entity).

These tests verify:
1. GET /oauth/authorize accepts request without state (no HTTP 422)
2. GET /oauth/authorize with state still works (backward compat)
3. POST /oauth/authorize/consent accepts request without state (no HTTP 422)
4. POST /oauth/authorize/consent redirect omits state when not provided
5. POST /oauth/authorize/consent redirect includes state when provided (backward compat)
"""

import base64
import hashlib
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette import status

from code_indexer.server.auth.oauth.oauth_manager import OAuthManager
from code_indexer.server.auth.oauth import routes
from code_indexer.server.auth.user_manager import UserManager, UserRole

# Test user credentials — ephemeral, non-production values
_TEST_USERNAME = "testuser"
_TEST_PASSWORD = secrets.token_urlsafe(16) + "!Aa1"


def _get_authorize(
    app: TestClient,
    client_id: str,
    code_challenge: str,
    state: Optional[str] = None,
) -> object:
    """Helper: GET /oauth/authorize with optional state."""
    params = {
        "client_id": client_id,
        "redirect_uri": "https://example.com/callback",
        "code_challenge": code_challenge,
        "response_type": "code",
    }
    if state is not None:
        params["state"] = state
    return app.get("/oauth/authorize", params=params, follow_redirects=False)


def _post_consent(
    app: TestClient,
    client_id: str,
    code_challenge: str,
    cookies: object,
    state: Optional[str] = None,
) -> object:
    """Helper: POST /oauth/authorize/consent with optional state."""
    data = {
        "client_id": client_id,
        "redirect_uri": "https://example.com/callback",
        "code_challenge": code_challenge,
        "response_type": "code",
        "consent": "allow",
    }
    if state is not None:
        data["state"] = state
    return app.post(
        "/oauth/authorize/consent",
        data=data,
        cookies=cookies,  # type: ignore[arg-type]
        follow_redirects=False,
    )


@pytest.mark.slow
class TestBug624StateOptional:
    """Tests that state parameter is optional per OAuth 2.1 with PKCE."""

    @pytest.fixture(autouse=True)
    def reset_rate_limiters(self):
        """Reset global rate limiters before each test."""
        from code_indexer.server.auth.oauth_rate_limiter import (
            oauth_token_rate_limiter,
            oauth_register_rate_limiter,
        )

        oauth_token_rate_limiter._attempts.clear()
        oauth_register_rate_limiter._attempts.clear()
        yield
        oauth_token_rate_limiter._attempts.clear()
        oauth_register_rate_limiter._attempts.clear()

    @pytest.fixture
    def temp_dir(self):
        """Create and clean up a temporary directory."""
        d = Path(tempfile.mkdtemp())
        yield d
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def oauth_manager(self, temp_dir):
        """Create OAuth manager with temp database."""
        db_path = str(temp_dir / "oauth.db")
        return OAuthManager(db_path=db_path, issuer="http://localhost:8000")

    @pytest.fixture
    def user_manager(self, temp_dir):
        """Create UserManager with a test user (ephemeral password)."""
        users_file = str(temp_dir / "users.json")
        um = UserManager(users_file_path=users_file)
        um.create_user(_TEST_USERNAME, _TEST_PASSWORD, UserRole.NORMAL_USER)
        return um

    @pytest.fixture
    def registered_client(self, oauth_manager):
        """Register a test OAuth client."""
        return oauth_manager.register_client(
            client_name="Test PKCE Client",
            redirect_uris=["https://example.com/callback"],
        )

    @pytest.fixture
    def pkce_pair(self):
        """Generate PKCE code verifier and challenge."""
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return verifier, challenge

    @pytest.fixture
    def app(self, oauth_manager, user_manager):
        """Create FastAPI app with OAuth routes wired with real components."""
        test_app = FastAPI()
        test_app.include_router(routes.router)
        test_app.dependency_overrides[routes.get_oauth_manager] = lambda: oauth_manager
        test_app.dependency_overrides[routes.get_user_manager] = lambda: user_manager
        return TestClient(test_app, raise_server_exceptions=False)

    @pytest.fixture
    def session_cookies(self):
        """Create a valid session cookie for testuser (NORMAL_USER role).

        The session manager must remain initialized throughout the test because
        the OAuth consent route calls get_session_manager() directly (not via
        dependency injection). We restore the previous global state after yield.
        """
        import code_indexer.server.web.auth as auth_module
        from code_indexer.server.web import init_session_manager, get_session_manager

        # Minimal config stub — only .host is needed for secure-cookie decision
        class _Config:
            host = "127.0.0.1"

        original = auth_module._session_manager
        init_session_manager("test-secret-key-for-unit-tests", _Config())

        helper_app = FastAPI()

        @helper_app.get("/make-session")
        async def make_session(request: Request):
            sm = get_session_manager()
            resp = JSONResponse({"ok": True})
            sm.create_session(resp, _TEST_USERNAME, "normal_user")
            return resp

        helper_client = TestClient(helper_app)
        session_resp = helper_client.get("/make-session")
        cookies = session_resp.cookies

        # Yield with session manager still active — the consent POST also calls
        # get_session_manager() directly and needs it initialized.
        yield cookies

        # Restore previous global state so we don't pollute other tests
        auth_module._session_manager = original

    # --- GET /oauth/authorize tests ---

    def test_get_authorize_without_state_does_not_return_422(
        self, app, registered_client, pkce_pair
    ):
        """GET /oauth/authorize without state must not return 422.

        Bug #624: state is optional per OAuth 2.1 PKCE. Missing state must not
        cause 422 Unprocessable Entity.
        """
        _, code_challenge = pkce_pair

        response = _get_authorize(app, registered_client["client_id"], code_challenge)

        assert response.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT, (  # type: ignore[attr-defined]
            f"HTTP 422 means state is still required. It must be optional per OAuth 2.1 PKCE. "  # type: ignore[attr-defined]
            f"Response: {response.text}"
        )

    def test_get_authorize_with_state_still_works(
        self, app, registered_client, pkce_pair
    ):
        """GET /oauth/authorize with state must still work (backward compatibility)."""
        _, code_challenge = pkce_pair

        response = _get_authorize(
            app, registered_client["client_id"], code_challenge, state="csrf_token_xyz"
        )

        assert (
            response.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT  # type: ignore[attr-defined]
        ), f"HTTP 422 with state provided — regression: {response.text}"  # type: ignore[attr-defined]

    # --- POST /oauth/authorize/consent tests ---

    def test_post_consent_without_state_does_not_return_422(
        self, app, registered_client, pkce_pair, session_cookies
    ):
        """POST /oauth/authorize/consent without state must not return 422.

        Bug #624: state is optional per OAuth 2.1 PKCE. Missing state must not
        cause 422 Unprocessable Entity on consent endpoint.
        """
        _, code_challenge = pkce_pair

        response = _post_consent(
            app, registered_client["client_id"], code_challenge, session_cookies
        )

        assert response.status_code != status.HTTP_422_UNPROCESSABLE_CONTENT, (  # type: ignore[attr-defined]
            f"HTTP 422 means state is still required. It must be optional per OAuth 2.1 PKCE. "  # type: ignore[attr-defined]
            f"Response: {response.text}"
        )

    def test_post_consent_redirect_omits_state_when_not_provided(
        self, app, registered_client, pkce_pair, session_cookies
    ):
        """POST /oauth/authorize/consent redirect URL must NOT include state=None.

        Bug #624: When state is omitted, redirect URL must not contain state=None
        or &state= which would confuse the OAuth client.
        """
        _, code_challenge = pkce_pair

        response = _post_consent(
            app, registered_client["client_id"], code_challenge, session_cookies
        )

        assert (
            response.status_code == status.HTTP_302_FOUND  # type: ignore[attr-defined]
        ), f"Expected redirect 302, got {response.status_code}: {response.text}"  # type: ignore[attr-defined]
        location = response.headers.get("location", "")  # type: ignore[attr-defined]
        assert "state=" not in location, (
            f"Redirect URL must not contain 'state=' when state was not provided: {location}"
        )
        assert "code=" in location, (
            f"Redirect URL must contain authorization code: {location}"
        )

    def test_post_consent_redirect_includes_state_when_provided(
        self, app, registered_client, pkce_pair, session_cookies
    ):
        """POST /oauth/authorize/consent redirect URL must include state when provided."""
        _, code_challenge = pkce_pair
        provided_state = "csrf_state_xyz789"

        response = _post_consent(
            app,
            registered_client["client_id"],
            code_challenge,
            session_cookies,
            state=provided_state,
        )

        assert (
            response.status_code == status.HTTP_302_FOUND  # type: ignore[attr-defined]
        ), f"Expected redirect 302, got {response.status_code}: {response.text}"  # type: ignore[attr-defined]
        location = response.headers.get("location", "")  # type: ignore[attr-defined]
        assert f"state={provided_state}" in location, (
            f"Redirect URL must echo back state when provided: {location}"
        )
        assert "code=" in location, (
            f"Redirect URL must contain authorization code: {location}"
        )


# ---------------------------------------------------------------------------
# Additional tests for 5 remaining state-optional locations (code review findings)
# ---------------------------------------------------------------------------


class TestBug624PostAuthorizeStateOptional:
    """POST /oauth/authorize (JSON body) — state must be optional.

    Finding 1: explicit `if not state: raise 400` guard.
    Finding 2: AuthorizeRequest Pydantic model has `state: str` (required).
    Both must be fixed so PKCE clients omitting state are accepted.
    """

    @pytest.fixture(autouse=True)
    def reset_rate_limiters(self):
        from code_indexer.server.auth.oauth_rate_limiter import (
            oauth_token_rate_limiter,
            oauth_register_rate_limiter,
        )

        oauth_token_rate_limiter._attempts.clear()
        oauth_register_rate_limiter._attempts.clear()
        yield
        oauth_token_rate_limiter._attempts.clear()
        oauth_register_rate_limiter._attempts.clear()

    @pytest.fixture
    def temp_dir(self):
        d = Path(tempfile.mkdtemp())
        yield d
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def oauth_manager(self, temp_dir):
        db_path = str(temp_dir / "oauth.db")
        return OAuthManager(db_path=db_path, issuer="http://localhost:8000")

    @pytest.fixture
    def user_manager(self, temp_dir):
        users_file = str(temp_dir / "users.json")
        um = UserManager(users_file_path=users_file)
        um.create_user(_TEST_USERNAME, _TEST_PASSWORD, UserRole.NORMAL_USER)
        return um

    @pytest.fixture
    def registered_client(self, oauth_manager):
        return oauth_manager.register_client(
            client_name="Test PKCE Client",
            redirect_uris=["https://example.com/callback"],
        )

    @pytest.fixture
    def pkce_pair(self):
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return verifier, challenge

    @pytest.fixture
    def app(self, oauth_manager, user_manager):
        test_app = FastAPI()
        test_app.include_router(routes.router)
        test_app.dependency_overrides[routes.get_oauth_manager] = lambda: oauth_manager
        test_app.dependency_overrides[routes.get_user_manager] = lambda: user_manager
        return TestClient(test_app, raise_server_exceptions=False)

    def test_post_authorize_json_without_state_does_not_return_400(
        self, app, registered_client, pkce_pair
    ):
        """POST /oauth/authorize (JSON) without state must not return 400 or 422.

        Bug #624 Findings 1+2: `if not state: raise 400` guard and
        `AuthorizeRequest.state: str` (required field) both reject requests
        without state. State must be optional per OAuth 2.1 PKCE.
        This test calls the real endpoint and will FAIL until both guards are fixed.
        """
        _, code_challenge = pkce_pair

        body = {
            "client_id": registered_client["client_id"],
            "redirect_uri": "https://example.com/callback",
            "code_challenge": code_challenge,
            "response_type": "code",
            "username": _TEST_USERNAME,
            "password": _TEST_PASSWORD,
            # state intentionally omitted
        }
        response = app.post("/oauth/authorize", json=body, follow_redirects=False)

        assert response.status_code not in (400, 422), (
            f"HTTP {response.status_code} means state is still required. "
            f"It must be optional per OAuth 2.1 PKCE. Response: {response.text}"
        )
        # Successful path: redirect (302) with code, no state= in URL
        if response.status_code == 302:
            location = response.headers.get("location", "")
            assert "code=" in location, (
                f"Redirect must contain authorization code: {location}"
            )
            assert "state=" not in location, (
                f"Redirect must NOT contain state= when state was not provided: {location}"
            )


@pytest.mark.slow
class TestBug624MfaVerifyStateOptional:
    """POST /oauth/mfa/verify — oauth_state=None must not block the flow.

    Finding 3: `if not challenge.oauth_client_id or not challenge.oauth_state`
    incorrectly rejects valid challenges when oauth_state is None.
    Finding 3 (redirect): lines 579-581 unconditionally append &state= to URL.
    """

    @pytest.fixture(autouse=True)
    def reset_rate_limiters(self):
        from code_indexer.server.auth.oauth_rate_limiter import (
            oauth_token_rate_limiter,
            oauth_register_rate_limiter,
        )

        oauth_token_rate_limiter._attempts.clear()
        oauth_register_rate_limiter._attempts.clear()
        yield
        oauth_token_rate_limiter._attempts.clear()
        oauth_register_rate_limiter._attempts.clear()

    @pytest.fixture
    def temp_dir(self):
        d = Path(tempfile.mkdtemp())
        yield d
        shutil.rmtree(d, ignore_errors=True)

    @pytest.fixture
    def oauth_manager(self, temp_dir):
        db_path = str(temp_dir / "oauth.db")
        return OAuthManager(db_path=db_path, issuer="http://localhost:8000")

    @pytest.fixture
    def user_manager(self, temp_dir):
        users_file = str(temp_dir / "users.json")
        um = UserManager(users_file_path=users_file)
        um.create_user(_TEST_USERNAME, _TEST_PASSWORD, UserRole.NORMAL_USER)
        return um

    @pytest.fixture
    def totp_service(self, temp_dir):
        from code_indexer.server.auth.totp_service import TOTPService

        db_path = str(temp_dir / "mfa.db")
        return TOTPService(db_path=db_path)

    @pytest.fixture
    def registered_client(self, oauth_manager):
        return oauth_manager.register_client(
            client_name="Test PKCE Client",
            redirect_uris=["https://example.com/callback"],
        )

    @pytest.fixture
    def pkce_pair(self):
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return verifier, challenge

    @pytest.fixture
    def app(self, oauth_manager, user_manager, totp_service):
        from code_indexer.server.web import mfa_routes

        test_app = FastAPI()
        test_app.include_router(routes.router)
        test_app.dependency_overrides[routes.get_oauth_manager] = lambda: oauth_manager
        test_app.dependency_overrides[routes.get_user_manager] = lambda: user_manager
        original_totp = mfa_routes._totp_service
        mfa_routes.set_totp_service(totp_service)
        yield TestClient(test_app, raise_server_exceptions=False)
        mfa_routes._totp_service = original_totp

    def _enable_mfa(self, totp_service) -> str:
        """Enable MFA for test user, return TOTP secret."""
        import pyotp
        import time as time_mod

        totp_service.generate_secret(_TEST_USERNAME)
        uri = totp_service.get_provisioning_uri(_TEST_USERNAME)
        assert uri is not None
        secret = uri.split("secret=")[1].split("&")[0]
        totp = pyotp.TOTP(secret)
        past_code = totp.at(int(time_mod.time()) - 30)
        result = totp_service.activate_mfa(_TEST_USERNAME, past_code)
        assert result is True
        return secret  # type: ignore[no-any-return]

    def _get_totp_code(self, secret: str) -> str:
        import pyotp

        return pyotp.TOTP(secret).now()

    def _create_challenge_token_without_state(
        self, registered_client, pkce_pair
    ) -> str:
        """Directly inject an MFA challenge with oauth_state=None into the manager."""
        from code_indexer.server.auth.mfa_challenge import mfa_challenge_manager

        _, code_challenge = pkce_pair
        return mfa_challenge_manager.create_challenge(  # type: ignore[no-any-return]
            username=_TEST_USERNAME,
            role=UserRole.NORMAL_USER.value,
            client_ip="testclient",
            redirect_url="/oauth/authorize",
            oauth_client_id=registered_client["client_id"],
            oauth_redirect_uri="https://example.com/callback",
            oauth_code_challenge=code_challenge,
            oauth_state=None,  # state is optional per OAuth 2.1 PKCE
        )

    def test_mfa_verify_without_oauth_state_succeeds_and_redirect_omits_state(
        self, app, registered_client, pkce_pair, totp_service
    ):
        """POST /oauth/mfa/verify with oauth_state=None must succeed and redirect without state=.

        Bug #624 Finding 3: The guard `not challenge.oauth_state` rejects valid
        OAuth MFA challenges when state is None. After fixing the guard, the
        redirect URL must not contain state= (Finding 3 redirect fix).
        This test calls the real endpoint and will FAIL until both fixes are applied.
        """
        secret = self._enable_mfa(totp_service)
        challenge_token = self._create_challenge_token_without_state(
            registered_client, pkce_pair
        )
        totp_code = self._get_totp_code(secret)

        response = app.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": challenge_token,
                "totp_code": totp_code,
            },
            follow_redirects=False,
        )

        assert response.status_code == 302, (
            f"Expected redirect 302 after MFA verify without state, "
            f"got {response.status_code}: {response.text}"
        )
        location = response.headers.get("location", "")
        assert location.startswith("https://example.com/callback"), (
            f"Redirect must go to OAuth client callback: {location}"
        )
        assert "code=" in location, (
            f"Redirect must contain authorization code: {location}"
        )
        assert "state=" not in location, (
            f"Redirect must NOT include state= when oauth_state was None: {location}"
        )


class TestBug624OidcCallbackStateOptional:
    """GET /auth/sso/callback with oauth_state=None — redirect must omit state=.

    Finding 4: oidc/routes.py line 93 unconditionally appends
    &state={state_data['oauth_state']} which produces state=None when absent.
    """

    def test_oidc_callback_oauth_flow_without_state_redirect_omits_state(self):
        """OIDC callback in OAuth flow with oauth_state=None must redirect without state=.

        Bug #624 Finding 4: The OIDC callback constructs
        `f"...?code={oauth_code}&state={state_data['oauth_state']}"` unconditionally.
        When oauth_state is None this produces state=None in the URL.
        This test calls the real endpoint and will FAIL until the fix is applied.
        """
        import tempfile
        import shutil
        from unittest.mock import Mock, AsyncMock
        from datetime import datetime, timezone
        from code_indexer.server.auth.oidc.routes import router as oidc_router
        from code_indexer.server.auth.oidc.oidc_manager import OIDCManager
        from code_indexer.server.auth.oidc.oidc_provider import (
            OIDCProvider,
            OIDCUserInfo,
        )
        from code_indexer.server.auth.oidc.state_manager import StateManager
        from code_indexer.server.utils.config_manager import OIDCProviderConfig
        from code_indexer.server.auth.user_manager import User, UserRole
        import code_indexer.server.auth.oidc.routes as oidc_routes_module

        # Set up a temporary OAuth manager for the OIDC callback to use
        tmp = Path(tempfile.mkdtemp())
        try:
            oauth_mgr = OAuthManager(
                db_path=str(tmp / "oauth.db"), issuer="http://localhost:8000"
            )
            client_info = oauth_mgr.register_client(
                client_name="OIDC Test Client",
                redirect_uris=["https://example.com/callback"],
            )

            verifier = secrets.token_urlsafe(64)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("=")
            )

            # Create OIDC state with oauth_state=None (PKCE without state)
            state_mgr = StateManager()
            state_token = state_mgr.create_state(
                {
                    "flow": "oauth_authorize",
                    "client_id": client_info["client_id"],
                    "code_challenge": challenge,
                    "redirect_uri": "https://example.com/callback",
                    "oauth_state": None,  # state is optional
                    "oidc_code_verifier": verifier,
                }
            )

            # Set up mock OIDC provider
            config = OIDCProviderConfig(
                enabled=True,
                issuer_url="https://example.com",
                client_id="test-client-id",
            )
            oidc_mgr = OIDCManager(config, None, None)
            oidc_mgr.provider = Mock(spec=OIDCProvider)
            oidc_mgr.provider.exchange_code_for_token = AsyncMock(
                return_value={
                    "access_token": "test-access-token",
                    "id_token": "test-id-token",
                }
            )
            oidc_mgr.provider.get_user_info = Mock(
                return_value=OIDCUserInfo(
                    subject="test-sub",
                    email="test@example.com",
                    email_verified=True,
                )
            )
            test_user = User(
                username=_TEST_USERNAME,
                role=UserRole.NORMAL_USER,
                password_hash="",
                created_at=datetime.now(timezone.utc),
                email="test@example.com",
            )
            oidc_mgr.match_or_create_user = AsyncMock(return_value=test_user)

            # Inject into OIDC routes module
            oidc_routes_module.oidc_manager = oidc_mgr
            oidc_routes_module.state_manager = state_mgr

            # Wire the oauth_manager into app.state (used by OIDC callback)
            oidc_app = FastAPI()
            oidc_app.include_router(oidc_router)
            oidc_app.state.oauth_manager = oauth_mgr

            client = TestClient(oidc_app, raise_server_exceptions=False)
            response = client.get(
                f"/auth/sso/callback?code=test-auth-code&state={state_token}",
                follow_redirects=False,
            )

            assert response.status_code == 302, (
                f"OIDC OAuth callback expected 302, got {response.status_code}: "
                f"{response.text}"
            )
            location = response.headers.get("location", "")
            assert location.startswith("https://example.com/callback"), (
                f"Redirect must go to OAuth client callback: {location}"
            )
            assert "code=" in location, (
                f"Redirect must contain authorization code: {location}"
            )
            assert "state=" not in location, (
                f"Redirect must NOT include state= when oauth_state was None: {location}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
