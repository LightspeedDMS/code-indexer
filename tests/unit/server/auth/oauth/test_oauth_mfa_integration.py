"""
Tests for OAuth MFA integration (Story #562).

Verifies that TOTP MFA is enforced during the OAuth authorization flow:
- Users without MFA: auth code issued immediately (unchanged behavior)
- Users with MFA: challenge page shown, TOTP verification required
- Client credentials: always bypass MFA (no user involved)
"""

import base64
import hashlib
import secrets
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.oauth.oauth_manager import OAuthManager
from code_indexer.server.auth.oauth import routes
from code_indexer.server.auth.user_manager import UserManager, UserRole


class TestOAuthMfaIntegration:
    """Test MFA enforcement in the OAuth authorization flow."""

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
        """Create UserManager with a test user."""
        users_file = str(temp_dir / "users.json")
        um = UserManager(users_file_path=users_file)
        um.create_user("testuser", "ValidPassword123!", UserRole.NORMAL_USER)
        return um

    @pytest.fixture
    def totp_service(self, temp_dir):
        """Create real TOTPService with temp database."""
        from code_indexer.server.auth.totp_service import TOTPService

        db_path = str(temp_dir / "mfa.db")
        return TOTPService(db_path=db_path)

    @pytest.fixture
    def registered_client(self, oauth_manager):
        """Register a test OAuth client."""
        return oauth_manager.register_client(
            client_name="Test MFA Client",
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
    def app(self, oauth_manager, user_manager, totp_service):
        """Create FastAPI app with OAuth routes and MFA wiring."""
        from code_indexer.server.web import mfa_routes

        app = FastAPI()
        app.include_router(routes.router)

        # Wire dependencies via overrides (not mocking)
        app.dependency_overrides[routes.get_oauth_manager] = lambda: oauth_manager
        app.dependency_overrides[routes.get_user_manager] = lambda: user_manager

        # Wire the TOTP service into mfa_routes module
        original_totp = mfa_routes._totp_service
        mfa_routes.set_totp_service(totp_service)

        yield TestClient(app)

        # Restore original
        mfa_routes._totp_service = original_totp

    def test_authorize_bypasses_mfa_when_not_enabled(
        self, app, registered_client, pkce_pair
    ):
        """POST /oauth/authorize issues auth code when user has no MFA."""
        _, code_challenge = pkce_pair

        response = app.post(
            "/oauth/authorize",
            data={
                "client_id": registered_client["client_id"],
                "redirect_uri": "https://example.com/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "state": "test_state",
                "username": "testuser",
                "password": "ValidPassword123!",
            },
            follow_redirects=False,
        )

        # Should redirect to callback with auth code (no MFA involved)
        assert response.status_code == 302
        location = response.headers["location"]
        assert "code=" in location
        assert "state=test_state" in location

    def _enable_mfa_for_user(self, totp_service, username: str) -> None:
        """Enable MFA for a user by generating secret and activating.

        Uses a past TOTP code (t-1 window) for activation so the
        current code remains usable for subsequent verification tests.
        """
        import pyotp
        import time as time_mod

        totp_service.generate_secret(username)
        uri = totp_service.get_provisioning_uri(username)
        assert uri is not None
        secret = uri.split("secret=")[1].split("&")[0]
        totp = pyotp.TOTP(secret)
        past_code = totp.at(int(time_mod.time()) - 30)
        result = totp_service.activate_mfa(username, past_code)
        assert result is True

    def test_authorize_shows_mfa_challenge_when_enabled(
        self, app, registered_client, pkce_pair, totp_service
    ):
        """POST /oauth/authorize returns MFA challenge when user has MFA."""
        _, code_challenge = pkce_pair
        self._enable_mfa_for_user(totp_service, "testuser")

        response = app.post(
            "/oauth/authorize",
            data={
                "client_id": registered_client["client_id"],
                "redirect_uri": "https://example.com/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "state": "test_state",
                "username": "testuser",
                "password": "ValidPassword123!",
            },
            follow_redirects=False,
        )

        # Should return MFA challenge page (not redirect with auth code)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        body = response.text
        assert "Two-Factor Authentication" in body
        assert "challenge_token" in body
        # The form should post to the OAuth-specific MFA verify endpoint
        assert "/oauth/mfa/verify" in body

    def _get_totp_code(self, totp_service, username: str) -> str:
        """Generate current TOTP code for a user."""
        import pyotp

        uri = totp_service.get_provisioning_uri(username)
        assert uri is not None
        secret = uri.split("secret=")[1].split("&")[0]
        return pyotp.TOTP(secret).now()

    def _create_oauth_mfa_challenge(
        self, totp_service, registered_client, pkce_pair, app
    ) -> str:
        """Trigger MFA challenge via POST /oauth/authorize and extract token."""
        _, code_challenge = pkce_pair
        self._enable_mfa_for_user(totp_service, "testuser")

        response = app.post(
            "/oauth/authorize",
            data={
                "client_id": registered_client["client_id"],
                "redirect_uri": "https://example.com/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "state": "test_state",
                "username": "testuser",
                "password": "ValidPassword123!",
            },
            follow_redirects=False,
        )
        assert response.status_code == 200
        # Extract challenge_token from the HTML form
        import re

        match = re.search(r"name='challenge_token'\s+value='([^']+)'", response.text)
        assert match is not None, "challenge_token not found in MFA page"
        return match.group(1)

    def test_oauth_mfa_verify_completes_authorization(
        self, app, registered_client, pkce_pair, totp_service, oauth_manager
    ):
        """POST /oauth/mfa/verify with valid TOTP completes OAuth flow."""
        challenge_token = self._create_oauth_mfa_challenge(
            totp_service, registered_client, pkce_pair, app
        )
        totp_code = self._get_totp_code(totp_service, "testuser")

        response = app.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": challenge_token,
                "totp_code": totp_code,
            },
            follow_redirects=False,
        )

        # Should redirect to client callback with auth code
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://example.com/callback")
        assert "code=" in location
        assert "state=test_state" in location

    def test_oauth_mfa_verify_rejects_invalid_totp(
        self, app, registered_client, pkce_pair, totp_service
    ):
        """POST /oauth/mfa/verify with wrong TOTP code returns 401."""
        challenge_token = self._create_oauth_mfa_challenge(
            totp_service, registered_client, pkce_pair, app
        )

        response = app.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": challenge_token,
                "totp_code": "000000",
            },
            follow_redirects=False,
        )

        assert response.status_code == 401
        assert "Invalid verification code" in response.json()["detail"]

    def test_oauth_mfa_verify_rejects_expired_token(self, app):
        """POST /oauth/mfa/verify with bogus token returns 400."""
        response = app.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": "bogus_token_that_does_not_exist",
                "totp_code": "123456",
            },
            follow_redirects=False,
        )

        assert response.status_code == 400
        assert "expired or invalid" in response.json()["detail"]

    def test_client_credentials_bypasses_mfa(
        self, oauth_manager, user_manager, totp_service, temp_dir
    ):
        """Client credentials grant never triggers MFA check.

        The /oauth/token endpoint with client_credentials grant
        authenticates via client_id/client_secret (not user password),
        so MFA is architecturally bypassed -- no user login involved.
        """
        from code_indexer.server.auth.mcp_credential_manager import (
            MCPCredentialManager,
        )
        from code_indexer.server.web import mfa_routes

        # Enable MFA for the user
        self._enable_mfa_for_user(totp_service, "testuser")

        # Create MCP credential
        mcp_mgr = MCPCredentialManager(user_manager=user_manager)
        cred = mcp_mgr.generate_credential(user_id="testuser", name="Test credential")

        # Build app with MCPCredentialManager wired
        app = FastAPI()
        app.include_router(routes.router)
        app.dependency_overrides[routes.get_oauth_manager] = lambda: oauth_manager
        app.dependency_overrides[routes.get_user_manager] = lambda: user_manager
        app.dependency_overrides[routes.get_mcp_credential_manager] = lambda: mcp_mgr
        original_totp = mfa_routes._totp_service
        mfa_routes.set_totp_service(totp_service)
        client = TestClient(app)

        try:
            response = client.post(
                "/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": cred["client_id"],
                    "client_secret": cred["client_secret"],
                },
            )

            # Should succeed -- MFA not checked for client credentials
            assert response.status_code == 200
            assert "access_token" in response.json()
        finally:
            mfa_routes._totp_service = original_totp

    def test_oauth_mfa_verify_with_recovery_code(
        self, app, registered_client, pkce_pair, totp_service
    ):
        """POST /oauth/mfa/verify with valid recovery code completes OAuth."""
        challenge_token = self._create_oauth_mfa_challenge(
            totp_service, registered_client, pkce_pair, app
        )
        # Generate recovery codes and use the first one
        codes = totp_service.generate_recovery_codes("testuser")
        assert codes is not None and len(codes) > 0

        response = app.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": challenge_token,
                "recovery_code": codes[0],
            },
            follow_redirects=False,
        )

        # Should redirect to client callback with auth code
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("https://example.com/callback")
        assert "code=" in location

    def test_authorize_json_shows_mfa_challenge_when_enabled(
        self, app, registered_client, pkce_pair, totp_service
    ):
        """JSON POST /oauth/authorize returns MFA challenge when user has MFA."""
        _, code_challenge = pkce_pair
        self._enable_mfa_for_user(totp_service, "testuser")

        response = app.post(
            "/oauth/authorize",
            json={
                "client_id": registered_client["client_id"],
                "redirect_uri": "https://example.com/callback",
                "response_type": "code",
                "code_challenge": code_challenge,
                "state": "test_state",
                "username": "testuser",
                "password": "ValidPassword123!",
            },
        )

        # Should return MFA challenge page even for JSON requests
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Two-Factor Authentication" in response.text
        assert "/oauth/mfa/verify" in response.text

    def test_oauth_mfa_verify_no_code_provided(
        self, app, registered_client, pkce_pair, totp_service
    ):
        """POST /oauth/mfa/verify without any code returns 401."""
        challenge_token = self._create_oauth_mfa_challenge(
            totp_service, registered_client, pkce_pair, app
        )

        response = app.post(
            "/oauth/mfa/verify",
            data={
                "challenge_token": challenge_token,
            },
            follow_redirects=False,
        )

        # No TOTP code and no recovery code = verification fails
        assert response.status_code == 401
        assert "Invalid verification code" in response.json()["detail"]
