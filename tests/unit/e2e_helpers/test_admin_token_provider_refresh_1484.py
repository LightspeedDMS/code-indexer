"""Unit tests for AdminTokenProvider's refresh-token-grant-first renewal (Bug #1484).

Bug #1484: in a long e2e Phase 3 run, the session-scoped admin token's
near-expiry renewal did a FULL username/password re-login. Late in a long,
auth-heavy phase (after user-deletion tests, destructive tests, etc.) that
re-login can 401 with otherwise-valid credentials -- consistent with
account-state/rate-limit accumulation tied to the LOGIN endpoint specifically.

Renewing via the refresh-token grant (POST /api/auth/refresh) instead avoids
resubmitting credentials entirely, so a routine token renewal cannot trip a
credential-based lockout. Full re-login remains the fallback when no refresh
grant is available or the grant itself fails.

These tests exercise AdminTokenProvider's pure Python renewal-selection logic
via injected callables -- no real HTTP/server involved, matching the existing
sibling test module (tests/e2e/server/test_admin_token_provider.py) which
tests the same class the same way. This file lives under tests/unit/e2e_helpers/
because it is a unit test of that class's logic, not an end-to-end test.
"""

from __future__ import annotations

from typing import Optional

from tests.e2e.server.conftest import AdminTokenProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_real_jwt(exp_offset_seconds: float) -> str:
    """Mint a real JWT that expires *exp_offset_seconds* from now."""
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


class TestAdminTokenProviderRefreshGrant:
    """Prove the refresh-token-grant-first renewal logic (Bug #1484)."""

    def test_refresh_grant_used_when_near_expiry_and_refresh_token_available(
        self,
    ) -> None:
        """Near-expiry + a refresh token + a working refresh_fn: no full re-login."""
        near_expiry_token = _make_real_jwt(30)
        renewed_access = _make_real_jwt(600)
        renewed_refresh = "renewed-refresh-token"

        login_call_count = 0
        refresh_calls: list = []

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            return _make_real_jwt(600), None

        def fake_refresh(refresh_token: str) -> Optional[tuple[str, Optional[str]]]:
            refresh_calls.append(refresh_token)
            return renewed_access, renewed_refresh

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=near_expiry_token,
            initial_refresh_token="initial-refresh-token",
            refresh_fn=fake_refresh,
        )

        token = provider.get_token()

        assert token == renewed_access, (
            "Near-expiry renewal should prefer the refresh-token grant"
        )
        assert refresh_calls == ["initial-refresh-token"], (
            f"refresh_fn should be called exactly once with the cached refresh "
            f"token; got {refresh_calls}"
        )
        assert login_call_count == 0, (
            f"Full re-login must NOT occur when the refresh grant succeeds; "
            f"got {login_call_count} login calls"
        )

    def test_falls_back_to_login_when_refresh_fn_returns_none(self) -> None:
        """A refresh grant failure (returns None) falls back to full re-login."""
        near_expiry_token = _make_real_jwt(30)
        new_token = _make_real_jwt(600)

        login_call_count = 0

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            return new_token, None

        def fake_refresh(refresh_token: str) -> Optional[tuple[str, Optional[str]]]:
            return None  # simulates a 401/429 on the refresh grant

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=near_expiry_token,
            initial_refresh_token="initial-refresh-token",
            refresh_fn=fake_refresh,
        )

        token = provider.get_token()

        assert token == new_token
        assert login_call_count == 1, (
            f"Full re-login must occur when the refresh grant fails; "
            f"got {login_call_count} login calls"
        )

    def test_falls_back_to_login_when_refresh_fn_raises(self) -> None:
        """A refresh grant that raises falls back to full re-login (bounded)."""
        near_expiry_token = _make_real_jwt(30)
        new_token = _make_real_jwt(600)

        login_call_count = 0

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            return new_token, None

        def fake_refresh(refresh_token: str) -> Optional[tuple[str, Optional[str]]]:
            raise RuntimeError("network error calling /api/auth/refresh")

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=near_expiry_token,
            initial_refresh_token="initial-refresh-token",
            refresh_fn=fake_refresh,
        )

        token = provider.get_token()

        assert token == new_token
        assert login_call_count == 1

    def test_falls_back_to_login_when_no_refresh_token_available(self) -> None:
        """With no cached refresh token, refresh_fn is never called; full re-login runs."""
        near_expiry_token = _make_real_jwt(30)
        new_token = _make_real_jwt(600)

        login_call_count = 0
        refresh_call_count = 0

        def fake_login() -> tuple[str, Optional[str]]:
            nonlocal login_call_count
            login_call_count += 1
            return new_token, None

        def fake_refresh(refresh_token: str) -> Optional[tuple[str, Optional[str]]]:
            nonlocal refresh_call_count
            refresh_call_count += 1
            return _make_real_jwt(600), "some-refresh-token"

        provider = AdminTokenProvider(
            login_fn=fake_login,
            initial_access_token=near_expiry_token,
            initial_refresh_token=None,
            refresh_fn=fake_refresh,
        )

        token = provider.get_token()

        assert token == new_token
        assert login_call_count == 1
        assert refresh_call_count == 0, (
            "refresh_fn must never be called without a cached refresh token"
        )

    def test_backward_compatible_without_refresh_fn(self) -> None:
        """Omitting refresh_fn entirely preserves the original full-re-login behavior."""
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
            initial_refresh_token="some-refresh-token",
        )

        token = provider.get_token()

        assert token == new_token
        assert login_call_count == 1
