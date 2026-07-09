"""Unit tests for AdminTokenProvider (token refresh on near-expiry).

These tests validate the core refresh logic without a running server:
  - A fresh token (far from expiry) is returned as-is.
  - A token within REFRESH_THRESHOLD_SECONDS of expiry triggers re-login.
  - A token past expiry also triggers re-login.

The token provider is tested via the real JWTManager to mint real JWTs
(no mocking of JWT creation).  The 're-login' is simulated by a fake
login function that records calls and returns a pre-canned new token.
"""

from __future__ import annotations

from typing import Optional


# Import the class under test -- will fail (RED) until implemented.
from tests.e2e.server.conftest import AdminTokenProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_real_jwt(exp_offset_seconds: float) -> str:
    """Mint a real JWT that expires *exp_offset_seconds* from now.

    Uses the real JWTManager so the token is structurally valid and the
    exp claim is the standard Unix timestamp.

    Args:
        exp_offset_seconds: Positive = future expiry; negative = already expired.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    from jose import jwt as jose_jwt

    secret = "test-secret-key"
    algorithm = "HS256"

    now = datetime.now(timezone.utc)
    expire = now + timedelta(seconds=exp_offset_seconds)

    payload = {
        "username": "admin",
        "role": "admin",
        "created_at": now.isoformat(),
        "exp": expire.timestamp(),
        "iat": now.timestamp(),
        "jti": str(uuid.uuid4()),
    }
    return str(jose_jwt.encode(payload, secret, algorithm=algorithm))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminTokenProviderRefreshLogic:
    """Prove the near-expiry refresh logic without a real server."""

    def test_fresh_token_returned_without_refresh(self) -> None:
        """A token 5 minutes from expiry is returned as-is (no re-login)."""
        fresh_token = _make_real_jwt(300)  # 5 minutes from now

        login_call_count = 0

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            # Returns (access_token, refresh_token)
            return _make_real_jwt(600), None

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=fresh_token,
            initial_refresh_token=None,
        )

        token = provider.get_token()
        assert token == fresh_token, "Fresh token should be returned unchanged"
        assert login_call_count == 0, (
            f"No re-login should occur for a fresh token; got {login_call_count} calls"
        )

    def test_near_expiry_token_triggers_relog(self) -> None:
        """A token within REFRESH_THRESHOLD_SECONDS of expiry triggers re-login."""
        # Token expires in 30 seconds -- within the 60-second refresh threshold
        near_expiry_token = _make_real_jwt(30)
        new_token = _make_real_jwt(600)

        login_call_count = 0

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            return new_token, None

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=near_expiry_token,
            initial_refresh_token=None,
        )

        token = provider.get_token()
        assert token == new_token, (
            f"Near-expiry token should trigger re-login; expected new token, got {token[:20]}..."
        )
        assert login_call_count == 1, (
            f"Exactly one re-login should occur; got {login_call_count}"
        )

    def test_expired_token_triggers_relog(self) -> None:
        """A token already past expiry also triggers re-login."""
        expired_token = _make_real_jwt(-60)  # expired 60 seconds ago
        new_token = _make_real_jwt(600)

        login_call_count = 0

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            return new_token, None

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=expired_token,
            initial_refresh_token=None,
        )

        token = provider.get_token()
        assert token == new_token, "Expired token should trigger re-login"
        assert login_call_count == 1

    def test_second_call_uses_cached_fresh_token(self) -> None:
        """After a re-login, the new token is cached and returned on subsequent calls."""
        near_expiry_token = _make_real_jwt(30)
        new_token = _make_real_jwt(600)

        login_call_count = 0

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            return new_token, None

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=near_expiry_token,
            initial_refresh_token=None,
        )

        token1 = provider.get_token()  # triggers re-login
        token2 = provider.get_token()  # should use cached new token

        assert token1 == new_token
        assert token2 == new_token
        assert login_call_count == 1, (
            f"Second call should use cached token; got {login_call_count} logins"
        )

    def test_get_headers_returns_authorization_header(self) -> None:
        """get_headers() returns the Authorization: Bearer header dict."""
        fresh_token = _make_real_jwt(300)

        def fake_login() -> tuple[str, Optional[str]]:
            return _make_real_jwt(600), None

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=fresh_token,
            initial_refresh_token=None,
        )

        headers = provider.get_headers()
        assert "Authorization" in headers
        assert headers["Authorization"] == f"Bearer {fresh_token}"
