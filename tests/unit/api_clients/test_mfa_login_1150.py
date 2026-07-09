"""Tests for Bug #1150 - MFA login flow in CIDXRemoteAPIClient._authenticate().

These tests verify that _authenticate() correctly handles a TOTP MFA challenge
returned by POST /auth/login, by following up with POST /auth/mfa/verify.

Server contract (mirrored exactly):
- POST /auth/login with MFA enabled returns:
    HTTP 200 {"mfa_required": true, "mfa_token": "<token>"}  (NO access_token)
- POST /auth/mfa/verify {"mfa_token": "<token>", "totp_code": "<6-digits>"} returns:
    HTTP 200 {"access_token": ..., "token_type": ..., ...}  on success
    HTTP 401 on wrong/expired code or token
    HTTP 503 if MFA service unavailable

All tests stub only self.session.post (the transport layer).
They do NOT mock _authenticate() itself.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


def _make_client(totp_provider=None):
    """Build a CIDXRemoteAPIClient with a stubbed session."""
    from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

    client = CIDXRemoteAPIClient(
        server_url="http://localhost:8000",
        credentials={"username": "admin", "password": "secret"},
        totp_provider=totp_provider,
    )
    return client


def _mock_response(status_code: int, json_data: Any) -> MagicMock:
    """Build a minimal mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# Test a: MFA challenge then successful verify
# ---------------------------------------------------------------------------


class TestMFALoginSuccessPath:
    """_authenticate() completes MFA challenge and returns access_token."""

    def test_mfa_challenge_then_successful_verify_returns_token(self) -> None:
        """
        When /auth/login returns mfa_required+mfa_token and /auth/mfa/verify
        returns access_token, _authenticate() must return that access_token.
        """
        totp_provider = lambda: "123456"  # noqa: E731
        client = _make_client(totp_provider=totp_provider)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok-abc"}
        )
        verify_response = _mock_response(
            200,
            {
                "access_token": "jwt-token-xyz",
                "token_type": "bearer",
                "user": {"username": "admin"},
            },
        )
        client._session = MagicMock()
        client._session.post.side_effect = [login_response, verify_response]
        client._session.is_closed = False

        token = client._authenticate()

        assert token == "jwt-token-xyz"

    def test_mfa_verify_called_with_correct_mfa_token_and_totp_code(self) -> None:
        """
        /auth/mfa/verify must be called with the mfa_token from the login
        response and the totp_code returned by the totp_provider.
        """
        totp_provider = lambda: "654321"  # noqa: E731
        client = _make_client(totp_provider=totp_provider)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "challenge-token-42"}
        )
        verify_response = _mock_response(
            200,
            {"access_token": "tok-success", "token_type": "bearer"},
        )
        client._session = MagicMock()
        client._session.post.side_effect = [login_response, verify_response]
        client._session.is_closed = False

        client._authenticate()

        # Second call must be to /auth/mfa/verify with the right body
        second_call = client._session.post.call_args_list[1]
        called_url = second_call[0][0]
        called_json = second_call[1].get("json", {})

        assert "/auth/mfa/verify" in called_url
        assert called_json.get("mfa_token") == "challenge-token-42"
        assert called_json.get("totp_code") == "654321"

    def test_totp_code_not_logged(self, caplog) -> None:
        """
        The TOTP code and mfa_token must never appear in log output
        (security requirement: no credential logging).
        """
        import logging

        totp_provider = lambda: "999888"  # noqa: E731
        client = _make_client(totp_provider=totp_provider)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "super-secret-mfa-tok"}
        )
        verify_response = _mock_response(
            200, {"access_token": "tok-ok", "token_type": "bearer"}
        )
        client._session = MagicMock()
        client._session.post.side_effect = [login_response, verify_response]
        client._session.is_closed = False

        with caplog.at_level(logging.DEBUG):
            client._authenticate()

        combined_log = " ".join(caplog.messages)
        assert "999888" not in combined_log, "TOTP code must not appear in logs"
        assert "super-secret-mfa-tok" not in combined_log, (
            "mfa_token must not appear in logs"
        )


# ---------------------------------------------------------------------------
# Test b: Wrong OTP (verify returns 401) → AuthenticationError, no hang
# ---------------------------------------------------------------------------


class TestMFALoginWrongOTP:
    """_authenticate() raises AuthenticationError on wrong TOTP code."""

    def test_wrong_totp_code_raises_authentication_error(self) -> None:
        """
        When /auth/mfa/verify returns 401, _authenticate() must raise
        AuthenticationError with a clear message (not hang or silently fail).
        """
        from code_indexer.api_clients.base_client import AuthenticationError

        totp_provider = lambda: "000000"  # noqa: E731
        client = _make_client(totp_provider=totp_provider)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok"}
        )
        verify_401 = _mock_response(
            401, {"detail": "Invalid TOTP code or expired token"}
        )
        client._session = MagicMock()
        client._session.post.side_effect = [login_response, verify_401]
        client._session.is_closed = False

        with pytest.raises(AuthenticationError) as exc_info:
            client._authenticate()

        assert exc_info.value is not None
        # Message must mention TOTP or code or invalid
        msg = str(exc_info.value).lower()
        assert any(word in msg for word in ("totp", "code", "invalid", "mfa"))

    def test_wrong_totp_does_not_hang(self) -> None:
        """_authenticate() terminates promptly on 401 verify (no infinite loop)."""
        from code_indexer.api_clients.base_client import AuthenticationError

        call_count = 0

        def counting_totp_provider() -> str:
            nonlocal call_count
            call_count += 1
            return "111111"

        client = _make_client(totp_provider=counting_totp_provider)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok"}
        )
        verify_401 = _mock_response(401, {"detail": "Invalid TOTP code"})
        client._session = MagicMock()
        client._session.post.side_effect = [login_response, verify_401]
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client._authenticate()

        # totp_provider called exactly once (single attempt, no re-prompt loop)
        assert call_count == 1

    def test_verify_503_raises_authentication_error(self) -> None:
        """
        When /auth/mfa/verify returns 503 (MFA service unavailable),
        _authenticate() raises AuthenticationError.
        """
        from code_indexer.api_clients.base_client import AuthenticationError

        totp_provider = lambda: "123456"  # noqa: E731
        client = _make_client(totp_provider=totp_provider)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok"}
        )
        verify_503 = _mock_response(503, {"detail": "MFA service unavailable"})
        client._session = MagicMock()
        client._session.post.side_effect = [login_response, verify_503]
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client._authenticate()


# ---------------------------------------------------------------------------
# Test c: MFA challenge but no totp_provider → clear AuthenticationError
# ---------------------------------------------------------------------------


class TestMFALoginNoProvider:
    """_authenticate() raises AuthenticationError with actionable message when no totp_provider."""

    def test_no_totp_provider_raises_authentication_error(self) -> None:
        """
        When MFA is required but no totp_provider is configured, _authenticate()
        must raise AuthenticationError with a clear actionable message.
        Must NOT hang, block, or prompt interactively.
        """
        from code_indexer.api_clients.base_client import AuthenticationError

        # No totp_provider — default None
        client = _make_client(totp_provider=None)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok"}
        )
        client._session = MagicMock()
        client._session.post.return_value = login_response
        client._session.is_closed = False

        with pytest.raises(AuthenticationError) as exc_info:
            client._authenticate()

        msg = str(exc_info.value).lower()
        # Message must be actionable: mention TOTP/MFA requirement
        assert any(
            word in msg
            for word in ("totp", "mfa", "interactive", "provider", "terminal")
        )

    def test_no_totp_provider_does_not_call_verify_endpoint(self) -> None:
        """
        When no totp_provider, /auth/mfa/verify must NOT be called
        (no half-formed request with empty code).
        """
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_client(totp_provider=None)

        login_response = _mock_response(
            200, {"mfa_required": True, "mfa_token": "mfa-tok"}
        )
        client._session = MagicMock()
        client._session.post.return_value = login_response
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client._authenticate()

        # Only one HTTP call (the login), no verify call
        assert client._session.post.call_count == 1


# ---------------------------------------------------------------------------
# Test d: Regression — normal login (access_token present) still works
# ---------------------------------------------------------------------------


class TestNormalLoginRegression:
    """Normal login path (access_token in first response) is not disturbed."""

    def test_normal_login_returns_token_without_mfa(self) -> None:
        """
        When /auth/login returns access_token directly (no MFA),
        _authenticate() returns it immediately and totp_provider is never called.
        """
        totp_called: list = []

        def totp_provider() -> str:
            totp_called.append(True)
            return "000000"

        client = _make_client(totp_provider=totp_provider)

        login_response = _mock_response(
            200,
            {
                "access_token": "direct-jwt-token",
                "token_type": "bearer",
                "user": {"username": "admin"},
            },
        )
        client._session = MagicMock()
        client._session.post.return_value = login_response
        client._session.is_closed = False

        token = client._authenticate()

        assert token == "direct-jwt-token"
        assert totp_called == [], "totp_provider must NOT be called on normal login"
        # Only one HTTP call — no verify endpoint hit
        assert client._session.post.call_count == 1

    def test_normal_login_without_provider_still_works(self) -> None:
        """Normal login (no MFA) must work even if totp_provider is None."""
        client = _make_client(totp_provider=None)

        login_response = _mock_response(
            200, {"access_token": "tok-normal", "token_type": "bearer"}
        )
        client._session = MagicMock()
        client._session.post.return_value = login_response
        client._session.is_closed = False

        token = client._authenticate()
        assert token == "tok-normal"

    def test_login_401_still_raises_authentication_error(self) -> None:
        """401 on /auth/login still raises AuthenticationError (existing behaviour preserved)."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_client(totp_provider=None)

        login_401 = _mock_response(401, {"detail": "Invalid credentials"})
        client._session = MagicMock()
        client._session.post.return_value = login_401
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client._authenticate()

    def test_login_non_mfa_200_missing_token_raises_error(self) -> None:
        """200 response without mfa_required and without access_token still raises error."""
        from code_indexer.api_clients.base_client import AuthenticationError

        client = _make_client(totp_provider=None)

        # Edge case: 200 with neither mfa_required nor access_token
        login_bad = _mock_response(200, {"status": "ok"})
        client._session = MagicMock()
        client._session.post.return_value = login_bad
        client._session.is_closed = False

        with pytest.raises(AuthenticationError):
            client._authenticate()
