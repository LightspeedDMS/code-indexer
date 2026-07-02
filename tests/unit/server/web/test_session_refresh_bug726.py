"""
Regression tests for Bug #726: Admin Web UI session not refreshed on activity.

Admin sessions expire after the 1-hour hard limit regardless of activity because
SessionManager.get_session() never re-issues the cookie.  These tests verify the
sliding-window (50%-lifetime) refresh introduced to fix that bug.

All tests use the REAL URLSafeTimedSerializer — no mocks of the signer.

Time control strategy
---------------------
Each time-sensitive call patches BOTH clocks:

1. ``itsdangerous.timed.time`` — controls the signer's embedded timestamp,
   which itsdangerous uses for expiry checks (``max_age``).
2. ``code_indexer.server.web.auth.time`` — controls the ``time.time()`` call
   in the SessionManager refresh helper that determines ``created_at`` and
   computes elapsed time for the 50%-lifetime threshold.

Patching both ensures a consistent view of "now" across signing, expiry
validation, and the refresh threshold calculation.

Secret key and CSRF tokens are generated at runtime via ``secrets.token_hex()``
— no hardcoded credential-like literals in source.
"""

import secrets
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from unittest.mock import MagicMock, patch

from itsdangerous import URLSafeTimedSerializer

from code_indexer.server.web.auth import (
    SessionManager,
    SESSION_COOKIE_NAME,
)

ADMIN_TIMEOUT = 3600  # 1 hour
USER_TIMEOUT = 28800  # 8 hours

# Generated at import time — not hardcoded literals.
_SECRET_KEY = secrets.token_hex(32)
_SALT = "web-session"


@dataclass
class FakeServerConfig:
    host: str = "127.0.0.1"


@dataclass
class FakeWebSecurityConfig:
    web_session_timeout_seconds: int = USER_TIMEOUT
    admin_session_timeout_seconds: int = ADMIN_TIMEOUT


def _make_manager(host: str = "127.0.0.1") -> SessionManager:
    """Return a SessionManager wired with fake configs."""
    return SessionManager(
        secret_key=_SECRET_KEY,
        config=FakeServerConfig(host=host),
        web_security_config=FakeWebSecurityConfig(),
    )


def _sign_session_at_wall_clock_time(
    sign_time: float,
    username: str = "admin",
    role: str = "admin",
    timeout: int = ADMIN_TIMEOUT,
    csrf_token: Optional[str] = None,
) -> str:
    """
    Create a signed session cookie whose itsdangerous signer timestamp AND
    ``created_at`` payload field are both set to ``sign_time``.

    Patches both ``itsdangerous.timed.time`` and ``code_indexer.server.web.auth.time``
    so every time consumer sees a consistent "now" during signing.
    """
    if csrf_token is None:
        csrf_token = secrets.token_hex(32)

    session_data = {
        "username": username,
        "role": role,
        "csrf_token": csrf_token,
        "created_at": sign_time,
        "session_timeout": timeout,
    }

    serializer = URLSafeTimedSerializer(_SECRET_KEY)
    with (
        patch("itsdangerous.timed.time") as mock_it_time,
        patch("code_indexer.server.web.auth.time") as mock_auth_time,
    ):
        mock_it_time.time.return_value = sign_time
        mock_auth_time.time.return_value = sign_time
        return serializer.dumps(session_data, salt=_SALT)


def _make_request(signed_cookie: str) -> MagicMock:
    request = MagicMock()
    request.cookies = {SESSION_COOKIE_NAME: signed_cookie}
    return request


def _make_response() -> MagicMock:
    response = MagicMock()
    response.set_cookie = MagicMock()
    return response


def _call_refresh(
    manager: SessionManager,
    signed_cookie: str,
    at_time: float,
) -> Tuple:
    """
    Call ``get_and_refresh_session`` with both clocks frozen at ``at_time``.

    Returns ``(session, response)`` so callers can inspect both.
    """
    request = _make_request(signed_cookie)
    response = _make_response()
    with (
        patch("itsdangerous.timed.time") as mock_it_time,
        patch("code_indexer.server.web.auth.time") as mock_auth_time,
    ):
        mock_it_time.time.return_value = at_time
        mock_auth_time.time.return_value = at_time
        session = manager.get_and_refresh_session(request, response)
    return session, response


# ---------------------------------------------------------------------------
# Test 1 — refresh fires past 50 % of lifetime
# ---------------------------------------------------------------------------


class TestAdminSessionRefreshPast50Percent:
    """Session past 50 % of lifetime must emit a new cookie."""

    def test_admin_session_refreshes_past_50_percent_lifetime(self):
        """
        When a valid admin session is read at 60 % of its lifetime (2160 s of
        3600 s), get_and_refresh_session must call response.set_cookie once
        with an updated created_at.
        """
        manager = _make_manager()
        base_time = time.time()
        sign_time = base_time - ADMIN_TIMEOUT * 0.60  # signed 2160 s ago

        signed = _sign_session_at_wall_clock_time(sign_time)
        session, response = _call_refresh(manager, signed, at_time=base_time)

        assert session is not None
        assert session.username == "admin"
        assert session.role == "admin"

        assert response.set_cookie.called, (
            "Expected set_cookie to be called (refresh past 50 % lifetime)"
        )

        call_kwargs = response.set_cookie.call_args.kwargs
        new_signed_value = call_kwargs["value"]

        serializer = URLSafeTimedSerializer(_SECRET_KEY)
        new_data = serializer.loads(new_signed_value, salt=_SALT)
        assert new_data["created_at"] > session.created_at, (
            "Refreshed cookie must have a newer created_at than the old one"
        )


# ---------------------------------------------------------------------------
# Test 2 — no refresh before 50 % threshold
# ---------------------------------------------------------------------------


class TestSessionNotRefreshedBefore50Percent:
    """Session under 50 % of lifetime must NOT emit a new cookie."""

    def test_session_not_refreshed_before_50_percent_threshold(self):
        """
        A session only 30 % through its lifetime must be returned as-is
        without calling response.set_cookie.
        """
        manager = _make_manager()
        base_time = time.time()
        sign_time = base_time - ADMIN_TIMEOUT * 0.30  # signed 1080 s ago

        signed = _sign_session_at_wall_clock_time(sign_time)
        session, response = _call_refresh(manager, signed, at_time=base_time)

        assert session is not None
        assert session.username == "admin"
        assert not response.set_cookie.called, (
            "set_cookie must NOT be called before 50 % threshold"
        )


# ---------------------------------------------------------------------------
# Test 3 — expired session returns None, no refresh attempted
# ---------------------------------------------------------------------------


class TestExpiredSessionReturnsNone:
    """Expired sessions must return None and never attempt a refresh."""

    def test_expired_session_returns_none(self):
        """
        A session whose signer timestamp is more than session_timeout seconds
        old must return None from get_and_refresh_session with no new cookie.
        """
        manager = _make_manager()
        base_time = time.time()
        sign_time = base_time - ADMIN_TIMEOUT * 1.10  # signed 3960 s ago — expired

        signed = _sign_session_at_wall_clock_time(sign_time)
        session, response = _call_refresh(manager, signed, at_time=base_time)

        assert session is None, "Expired session must return None"
        assert not response.set_cookie.called, (
            "No cookie must be set for an expired session"
        )


# ---------------------------------------------------------------------------
# Test 4 — CSRF token preserved across refresh
# ---------------------------------------------------------------------------


class TestCsrfTokenPreservedAcrossRefresh:
    """The CSRF token in the refreshed cookie must be identical to the original."""

    def test_csrf_token_preserved_across_refresh(self):
        """
        After a refresh the new signed cookie must contain the same CSRF token
        as the original session so that in-flight forms don't break.
        """
        manager = _make_manager()
        base_time = time.time()
        sign_time = base_time - ADMIN_TIMEOUT * 0.60
        csrf = secrets.token_hex(32)

        signed = _sign_session_at_wall_clock_time(sign_time, csrf_token=csrf)
        session, response = _call_refresh(manager, signed, at_time=base_time)

        assert session is not None
        assert session.csrf_token == csrf

        call_kwargs = response.set_cookie.call_args.kwargs
        new_signed_value = call_kwargs["value"]

        serializer = URLSafeTimedSerializer(_SECRET_KEY)
        new_data = serializer.loads(new_signed_value, salt=_SALT)

        assert new_data["csrf_token"] == csrf, (
            "CSRF token must be identical before and after refresh"
        )


# ---------------------------------------------------------------------------
# Test 5a — security flags preserved on refreshed cookie (localhost)
# ---------------------------------------------------------------------------


class TestRefreshedCookieSecurityFlagsLocalhost:
    """httponly / secure / samesite flags must be correct for localhost."""

    def test_refreshed_cookie_has_correct_security_flags_localhost(self):
        """
        For a localhost server (secure=False) the refreshed cookie must have
        httponly=True, secure=False, samesite='lax'.
        """
        manager = _make_manager(host="127.0.0.1")
        base_time = time.time()
        sign_time = base_time - ADMIN_TIMEOUT * 0.60

        signed = _sign_session_at_wall_clock_time(sign_time)
        session, response = _call_refresh(manager, signed, at_time=base_time)

        assert session is not None
        assert response.set_cookie.called

        call_kwargs = response.set_cookie.call_args.kwargs
        assert call_kwargs.get("httponly") is True, "httponly must be True"
        assert call_kwargs.get("secure") is False, "secure must be False on localhost"
        assert call_kwargs.get("samesite") == "lax", "samesite must be 'lax'"


# ---------------------------------------------------------------------------
# Test 5b — security flags preserved on refreshed cookie (production)
# ---------------------------------------------------------------------------


class TestRefreshedCookieSecurityFlagsProduction:
    """httponly / secure / samesite flags must be correct for production host."""

    def test_refreshed_cookie_has_correct_security_flags_production(self):
        """
        For a production server (secure=True) the refreshed cookie must have
        secure=True in addition to httponly=True and samesite='lax'.
        """
        manager = _make_manager(host="0.0.0.0")
        base_time = time.time()
        sign_time = base_time - ADMIN_TIMEOUT * 0.60

        signed = _sign_session_at_wall_clock_time(sign_time)
        session, response = _call_refresh(manager, signed, at_time=base_time)

        assert session is not None
        assert response.set_cookie.called

        call_kwargs = response.set_cookie.call_args.kwargs
        assert call_kwargs.get("secure") is True, "secure must be True in production"
        assert call_kwargs.get("httponly") is True, "httponly must be True"
        assert call_kwargs.get("samesite") == "lax", "samesite must be 'lax'"


# ---------------------------------------------------------------------------
# Test 6 — continuous activity keeps admin alive past the hard timeout
# ---------------------------------------------------------------------------


class TestContinuousActivityKeepsAdminAlive:
    """
    Unit test simulating continuous admin activity over 4 hours.

    Each cycle advances the simulated wall clock by 30 minutes and calls
    ``get_and_refresh_session`` with both clocks frozen at that new time.
    The test only replaces ``current_signed`` with the exact cookie string
    returned by ``response.set_cookie`` — the test never calls
    ``serializer.dumps()`` itself inside the request loop.
    """

    def test_continuous_activity_keeps_admin_alive_past_timeout(self):
        """
        Eight request cycles spaced 30 minutes apart (4 hours total, 4x the
        1-hour admin timeout) — each one past the 50 % threshold — must all
        return a valid session.
        """
        manager = _make_manager()

        # T=0: initial cookie signed right now.
        now = time.time()
        current_signed = _sign_session_at_wall_clock_time(sign_time=now)

        for i in range(8):
            # Advance simulated wall clock by 30 minutes.
            now += 30 * 60

            session, response = _call_refresh(manager, current_signed, at_time=now)

            assert session is not None, (
                f"Cycle {i + 1}: session must be valid (active user must not be logged out)"
            )
            assert response.set_cookie.called, (
                f"Cycle {i + 1}: cookie must be refreshed (30 min > 50 % of 1 h)"
            )

            # Use only the cookie the server issued — the test never re-signs.
            call_kwargs = response.set_cookie.call_args.kwargs
            current_signed = call_kwargs["value"]

        # Final validation: the last server-issued cookie was signed at ``now``.
        # Checking at the same ``now`` means age < 1 s — no further refresh.
        session, _ = _call_refresh(manager, current_signed, at_time=now)
        assert session is not None, (
            "After 4 hours of continuous activity the session must still be valid"
        )
