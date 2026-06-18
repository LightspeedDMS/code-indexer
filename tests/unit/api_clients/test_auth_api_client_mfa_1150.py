"""Tests for Bug #1150 - AuthAPIClient MFA wiring (auth_client layer).

Verifies that:
1. AuthAPIClient.__init__ accepts totp_provider and forwards it to
   CIDXRemoteAPIClient so that MFA-enrolled logins work end-to-end.
2. AuthAPIClient.login() handles the MFA challenge (mfa_required+mfa_token
   200 response) by delegating to _complete_mfa_challenge() and returning
   an AuthResponse with the resulting access_token.
3. Edge-case: verify 200 with missing/non-str access_token raises
   AuthenticationError (not a silent TypeError).

Transport is stubbed (session.post mock); no real HTTP is made.
_complete_mfa_challenge is NOT mocked — the real implementation is exercised.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _mock_response(status_code: int, json_data: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


def _stub_session(client: Any, responses: list) -> None:
    """Replace client._session with a mock whose post() yields responses."""
    client._session = MagicMock()
    client._session.post.side_effect = responses
    client._session.is_closed = False


# ---------------------------------------------------------------------------
# 1. AuthAPIClient constructor must accept totp_provider without TypeError
# ---------------------------------------------------------------------------


class TestAuthAPIClientAcceptsToTPProvider:
    """AuthAPIClient.__init__ must forward totp_provider to the base class."""

    def test_constructor_accepts_totp_provider_without_type_error(self) -> None:
        """
        Constructing AuthAPIClient with totp_provider=<callable> must NOT
        raise TypeError.  Before the fix this fails because super().__init__()
        does not forward the kwarg to CIDXRemoteAPIClient.
        """
        from code_indexer.api_clients.auth_client import AuthAPIClient

        provider = lambda: "123456"  # noqa: E731
        # Must not raise TypeError
        client = AuthAPIClient(
            server_url="http://localhost:8000",
            totp_provider=provider,
        )
        assert client is not None

    def test_totp_provider_stored_on_base_class(self) -> None:
        """
        After construction the provider must be accessible as _totp_provider
        (the attribute name the base class uses to call it).
        """
        from code_indexer.api_clients.auth_client import AuthAPIClient

        provider = lambda: "654321"  # noqa: E731
        client = AuthAPIClient(
            server_url="http://localhost:8000",
            totp_provider=provider,
        )
        assert client._totp_provider is provider

    def test_none_totp_provider_is_accepted(self) -> None:
        """Passing totp_provider=None must work (the pre-MFA default)."""
        from code_indexer.api_clients.auth_client import AuthAPIClient

        client = AuthAPIClient(
            server_url="http://localhost:8000",
            totp_provider=None,
        )
        assert client._totp_provider is None

    def test_omitting_totp_provider_defaults_to_none(self) -> None:
        """Omitting totp_provider must default to None (backward-compat)."""
        from code_indexer.api_clients.auth_client import AuthAPIClient

        client = AuthAPIClient(server_url="http://localhost:8000")
        assert client._totp_provider is None


# ---------------------------------------------------------------------------
# 2. AuthAPIClient.login() must handle the MFA challenge branch
# ---------------------------------------------------------------------------


class TestAuthAPIClientLoginMFA:
    """AuthAPIClient.login() must complete MFA when server returns mfa_required."""

    def test_login_mfa_challenge_with_provider_returns_auth_response(self) -> None:
        """
        When /auth/login returns mfa_required+mfa_token (no access_token) and
        /auth/mfa/verify returns access_token, login() must return an
        AuthResponse whose access_token matches the verify response.
        """
        from code_indexer.api_clients.auth_client import AuthAPIClient

        provider = lambda: "111222"  # noqa: E731
        client = AuthAPIClient(
            server_url="http://localhost:8000",
            totp_provider=provider,
        )

        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-login"}
        )
        verify_resp = _mock_response(
            200,
            {
                "access_token": "jwt-from-mfa",
                "token_type": "bearer",
                "user_id": "u-42",
            },
        )
        _stub_session(client, [login_resp, verify_resp])

        result = client.login("admin", "password")

        assert result["access_token"] == "jwt-from-mfa"

    def test_login_mfa_challenge_stores_credentials_when_project_root_set(
        self, tmp_path
    ) -> None:
        """
        After successful MFA login, credentials must be stored securely
        when project_root is provided (same behaviour as normal login).
        """
        from code_indexer.api_clients.auth_client import AuthAPIClient

        provider = lambda: "333444"  # noqa: E731
        client = AuthAPIClient(
            server_url="http://localhost:8000",
            project_root=tmp_path,
            totp_provider=provider,
        )

        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-store"}
        )
        verify_resp = _mock_response(
            200,
            {"access_token": "jwt-stored", "token_type": "bearer"},
        )
        _stub_session(client, [login_resp, verify_resp])

        # Should not raise
        result = client.login("admin", "pass")
        assert result["access_token"] == "jwt-stored"
        # After MFA login credentials are updated on the client
        assert client.credentials.get("username") == "admin"

    def test_login_mfa_challenge_no_provider_raises_authentication_error(
        self,
    ) -> None:
        """
        When MFA is required but totp_provider is None, login() must raise
        AuthenticationError with an actionable message (not hang or TypeError).
        """
        from code_indexer.api_clients.auth_client import (
            AuthAPIClient,
            AuthenticationError,
        )

        client = AuthAPIClient(
            server_url="http://localhost:8000",
            totp_provider=None,
        )
        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-no-provider"}
        )
        client._session = MagicMock()
        client._session.post.return_value = login_resp
        client._session.is_closed = False

        with pytest.raises(AuthenticationError) as exc_info:
            client.login("admin", "password")

        msg = str(exc_info.value).lower()
        assert any(
            word in msg
            for word in ("totp", "mfa", "interactive", "provider", "terminal")
        )

    def test_login_mfa_wrong_totp_raises_authentication_error(self) -> None:
        """Wrong TOTP code (verify returns 401) must raise AuthenticationError."""
        from code_indexer.api_clients.auth_client import (
            AuthAPIClient,
            AuthenticationError,
        )

        provider = lambda: "000000"  # noqa: E731
        client = AuthAPIClient(
            server_url="http://localhost:8000",
            totp_provider=provider,
        )

        login_resp = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-wrong"}
        )
        verify_401 = _mock_response(401, {"detail": "Invalid TOTP code"})
        _stub_session(client, [login_resp, verify_401])

        with pytest.raises(AuthenticationError):
            client.login("admin", "password")


# ---------------------------------------------------------------------------
# 3. Edge case: verify 200 with missing/non-str access_token raises error
# ---------------------------------------------------------------------------


class TestMFAVerifyMalformedSuccess:
    """_complete_mfa_challenge must raise AuthenticationError if 200 body lacks access_token."""

    def test_verify_200_missing_access_token_raises_authentication_error(
        self,
    ) -> None:
        """
        If POST /auth/mfa/verify returns HTTP 200 but the body has no
        access_token field, _complete_mfa_challenge must raise
        AuthenticationError (not return None and cause a later crash).
        """
        from code_indexer.api_clients.base_client import (
            CIDXRemoteAPIClient,
            AuthenticationError,
        )

        client = CIDXRemoteAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "secret"},
            totp_provider=lambda: "123456",
        )
        # Simulate: login triggers MFA, verify returns 200 with missing token
        login_resp = _mock_response(200, {"mfa_required": True, "mfa_token": "mfa-tok"})
        verify_bad_200 = _mock_response(200, {"status": "ok"})  # no access_token
        client._session = MagicMock()
        client._session.post.side_effect = [login_resp, verify_bad_200]
        client._session.is_closed = False

        with pytest.raises(AuthenticationError) as exc_info:
            client._authenticate()

        msg = str(exc_info.value).lower()
        assert any(word in msg for word in ("access_token", "token", "mfa", "verify"))

    def test_verify_200_null_access_token_raises_authentication_error(
        self,
    ) -> None:
        """
        If POST /auth/mfa/verify returns HTTP 200 with access_token=null,
        _complete_mfa_challenge must raise AuthenticationError.
        """
        from code_indexer.api_clients.base_client import (
            CIDXRemoteAPIClient,
            AuthenticationError,
        )

        client = CIDXRemoteAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "secret"},
            totp_provider=lambda: "123456",
        )
        login_resp = _mock_response(200, {"mfa_required": True, "mfa_token": "mfa-tok"})
        verify_null = _mock_response(
            200, {"access_token": None, "token_type": "bearer"}
        )
        client._session = MagicMock()
        client._session.post.side_effect = [login_resp, verify_null]
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client._authenticate()

    def test_verify_200_non_str_access_token_raises_authentication_error(
        self,
    ) -> None:
        """
        If POST /auth/mfa/verify returns HTTP 200 with access_token=<int>,
        _complete_mfa_challenge must raise AuthenticationError (type guard).
        """
        from code_indexer.api_clients.base_client import (
            CIDXRemoteAPIClient,
            AuthenticationError,
        )

        client = CIDXRemoteAPIClient(
            server_url="http://localhost:8000",
            credentials={"username": "admin", "password": "secret"},
            totp_provider=lambda: "123456",
        )
        login_resp = _mock_response(200, {"mfa_required": True, "mfa_token": "mfa-tok"})
        verify_int = _mock_response(
            200, {"access_token": 12345, "token_type": "bearer"}
        )
        client._session = MagicMock()
        client._session.post.side_effect = [login_resp, verify_int]
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client._authenticate()
