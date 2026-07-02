"""
Integration regression tests for Bug #726 (second attempt): session refresh wired
through FastAPI dependencies.

v9.17.6 shipped ``SessionManager.get_and_refresh_session()`` with correct sliding-
window logic, but ``require_admin_session`` and ``require_user_session`` still called
the non-refreshing ``get_session()``.  These tests drive real HTTP requests through a
FastAPI ``TestClient`` so that the FastAPI dependency-injection machinery is exercised
end-to-end — unlike the unit tests in ``test_session_refresh_bug726.py`` which call
``get_and_refresh_session`` directly.

Why these tests complement the unit tests in test_session_refresh_bug726.py
---------------------------------------------------------------------------
The unit tests call ``get_and_refresh_session`` directly and verify SessionManager
behaviour in isolation.  These integration tests call ``require_admin_session`` /
``require_user_session`` via FastAPI Depends — the layer that was actually broken:
the refresh method existed but was never invoked by the dependencies.

Time strategy
-------------
Use ``admin_session_timeout_seconds=10`` and ``web_session_timeout_seconds=20`` so
that each sleep has a full second of margin on both sides of the integer-second
boundary used by itsdangerous.  Named sleep constants make the intent explicit.
No mocks are used anywhere — these tests exercise the real system.

Timing margins (itsdangerous uses ``int(time.time())`` for timestamps):
  - Admin 50 % threshold = 5.0 s  →  sleep  6 s  (1 s margin)
  - User  50 % threshold = 10.0 s →  sleep 12 s  (2 s margin)
  - Admin expiry at 10 s          →  sleep 11 s  (1 s margin past timeout)

Markers
-------
``@pytest.mark.slow`` + ``@pytest.mark.integration``: excluded from fast-automation.sh
so the suite wall-clock time is not affected.  Run individually with::

    pytest tests/unit/server/web/test_session_refresh_integration_bug726.py -v
"""

import re
import secrets
import time
from dataclasses import dataclass
from typing import Generator, Tuple

import pytest
from fastapi import Depends, FastAPI, Response as FResponse
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from code_indexer.server.web.auth import (
    SESSION_COOKIE_NAME,
    SessionManager,
    init_session_manager,
    require_admin_session,
    require_user_session,
)

# ---------------------------------------------------------------------------
# Session timeout constants (large enough to give 1 s integer-safe margin)
# ---------------------------------------------------------------------------

ADMIN_TIMEOUT = 10  # seconds; 50 % threshold = 5.0 s
USER_TIMEOUT = 20  # seconds; 50 % threshold = 10.0 s

# Named sleep constants — each is 1+ seconds past the relevant threshold so
# itsdangerous integer-second rounding cannot cause a boundary failure.
ADMIN_REFRESH_DELAY = 6  # > ADMIN_TIMEOUT * 0.5 = 5.0 s  (1 s margin)
USER_REFRESH_DELAY = 12  # > USER_TIMEOUT * 0.5 = 10.0 s  (2 s margin)
ADMIN_EXPIRY_DELAY = 11  # > ADMIN_TIMEOUT = 10.0 s        (1 s margin past timeout)

_SALT = "web-session"


# ---------------------------------------------------------------------------
# Fake config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class _FakeServerConfig:
    """Minimal server config for SessionManager (localhost -> secure=False)."""

    host: str = "127.0.0.1"


@dataclass
class _FakeWebSecurityConfig:
    """Session timeouts matching the ADMIN_TIMEOUT / USER_TIMEOUT constants."""

    web_session_timeout_seconds: int = USER_TIMEOUT
    admin_session_timeout_seconds: int = ADMIN_TIMEOUT


# ---------------------------------------------------------------------------
# Test infrastructure helpers
# ---------------------------------------------------------------------------


def _build_app_and_client(secret_key: str) -> Tuple[TestClient, SessionManager]:
    """
    Build a minimal FastAPI app with two protected routes and return
    ``(TestClient, SessionManager)``.

    ``init_session_manager`` wires the global singleton that
    ``require_admin_session`` / ``require_user_session`` resolve via
    ``get_session_manager()``.

    The caller is responsible for calling ``client.close()`` to release transport
    and app-lifespan resources.
    """
    mgr = init_session_manager(
        secret_key=secret_key,
        config=_FakeServerConfig(),
        web_security_config=_FakeWebSecurityConfig(),
    )

    app = FastAPI()

    @app.get("/admin/ping")
    def admin_ping(session=Depends(require_admin_session)):
        return {"status": "ok", "user": session.username, "role": session.role}

    @app.get("/user/ping")
    def user_ping(session=Depends(require_user_session)):
        return {"status": "ok", "user": session.username, "role": session.role}

    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    return client, mgr


def _create_session_cookie(
    mgr: SessionManager, username: str, role: str
) -> Tuple[str, str]:
    """
    Create a real session via SessionManager and return ``(cookie_value, csrf_token)``.

    Extracts the signed cookie string from the response Set-Cookie header so it
    can be injected into TestClient requests.
    """
    resp_obj = FResponse()
    csrf_token = mgr.create_session(resp_obj, username, role)
    set_cookie = resp_obj.headers.get("set-cookie", "")
    match = re.search(rf"{SESSION_COOKIE_NAME}=([^;]+)", set_cookie)
    assert match, f"Could not find {SESSION_COOKIE_NAME}= in Set-Cookie: {set_cookie}"
    return match.group(1), csrf_token


def _get_with_session(
    client: TestClient,
    path: str,
    cookie_val: str,
    *,
    delay: int = 0,
):
    """
    Optionally sleep ``delay`` seconds then issue ``GET path`` with the session cookie.

    Returns the ``httpx.Response`` for assertion by the caller.
    """
    if delay > 0:
        time.sleep(delay)
    return client.get(path, cookies={SESSION_COOKIE_NAME: cookie_val})


@pytest.fixture()
def integration_setup() -> Generator[
    Tuple[TestClient, SessionManager, str], None, None
]:
    """
    Yield ``(client, session_manager, secret_key)`` for each test.

    Fresh secret key and SessionManager per test ensure full isolation.
    The TestClient is closed in a ``finally`` block for deterministic resource cleanup.
    """
    secret_key = secrets.token_hex(32)
    client, mgr = _build_app_and_client(secret_key)
    try:
        yield client, mgr, secret_key
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Test class 1: admin route refresh behaviour
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
class TestAdminRouteRefresh:
    """Verify that require_admin_session triggers sliding-window cookie refresh."""

    def test_refreshes_cookie_past_50_percent_threshold(
        self, integration_setup: Tuple[TestClient, SessionManager, str]
    ):
        """
        Primary regression test for Bug #726.

        A GET to /admin/ping after consuming >50 % of the admin timeout MUST
        produce a Set-Cookie header so the session window slides.
        Sleep 6 s gives 1 s margin past the 5 s threshold, safe for
        itsdangerous integer-second rounding.
        """
        client, mgr, _ = integration_setup
        cookie_val, _ = _create_session_cookie(mgr, "admin", "admin")

        response = _get_with_session(
            client, "/admin/ping", cookie_val, delay=ADMIN_REFRESH_DELAY
        )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )
        assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", ""), (
            "require_admin_session MUST issue a fresh Set-Cookie when the session "
            "is past 50 % of its lifetime (Bug #726 regression). "
            f"Response headers: {dict(response.headers)}"
        )

    def test_does_not_refresh_cookie_under_50_percent_threshold(
        self, integration_setup: Tuple[TestClient, SessionManager, str]
    ):
        """
        A GET immediately after cookie creation (well under the 5 s threshold)
        MUST NOT produce a Set-Cookie header.
        """
        client, mgr, _ = integration_setup
        cookie_val, _ = _create_session_cookie(mgr, "admin", "admin")

        response = _get_with_session(client, "/admin/ping", cookie_val)

        assert response.status_code == 200
        assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", ""), (
            "require_admin_session must NOT issue Set-Cookie before the 50 % threshold. "
            f"Unexpected Set-Cookie: {response.headers.get('set-cookie', '')}"
        )


# ---------------------------------------------------------------------------
# Test class 2: user route and expired session
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
class TestUserRouteAndExpiredSession:
    """Verify user-route refresh and expired-session redirect behaviour."""

    def test_user_route_refreshes_cookie_past_50_percent(
        self, integration_setup: Tuple[TestClient, SessionManager, str]
    ):
        """
        A GET to /user/ping after >50 % of the user timeout (>10 s of 20 s) MUST
        produce a Set-Cookie header — verifying the fix covers require_user_session.
        Sleep 12 s gives 2 s margin past the 10 s threshold.
        """
        client, mgr, _ = integration_setup
        cookie_val, _ = _create_session_cookie(mgr, "regular_user", "user")

        response = _get_with_session(
            client, "/user/ping", cookie_val, delay=USER_REFRESH_DELAY
        )

        assert response.status_code == 200, (
            f"Expected 200 OK, got {response.status_code}: {response.text}"
        )
        assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", ""), (
            "require_user_session MUST issue a fresh Set-Cookie when the session "
            "is past 50 % of its lifetime. "
            f"Response headers: {dict(response.headers)}"
        )

    def test_expired_session_returns_303_redirect_no_cookie_issued(
        self, integration_setup: Tuple[TestClient, SessionManager, str]
    ):
        """
        A request with a fully expired session MUST return 303 redirect to /login
        and MUST NOT issue a Set-Cookie header.

        Sleep 11 s gives 1 s margin past the 10 s ADMIN_TIMEOUT, ensuring
        itsdangerous integer-second rounding cannot keep the session alive.
        """
        client, mgr, _ = integration_setup
        cookie_val, _ = _create_session_cookie(mgr, "admin", "admin")

        response = _get_with_session(
            client, "/admin/ping", cookie_val, delay=ADMIN_EXPIRY_DELAY
        )

        assert response.status_code == 303, (
            f"Expected 303 redirect for expired session, got {response.status_code}"
        )
        assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", ""), (
            "Must NOT issue Set-Cookie for an expired session. "
            f"Unexpected header: {response.headers.get('set-cookie', '')}"
        )


# ---------------------------------------------------------------------------
# Test class 3: CSRF token preservation
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
class TestCsrfTokenPreservationAcrossRefresh:
    """Verify that the CSRF token is preserved in the refreshed cookie."""

    def test_csrf_token_preserved_across_route_refresh(
        self, integration_setup: Tuple[TestClient, SessionManager, str]
    ):
        """
        After a refresh triggered by the FastAPI dependency, the new cookie MUST
        contain the same CSRF token as the original session so in-flight forms
        are not broken.
        """
        client, mgr, secret_key = integration_setup
        cookie_val, original_csrf = _create_session_cookie(mgr, "admin", "admin")

        response = _get_with_session(
            client, "/admin/ping", cookie_val, delay=ADMIN_REFRESH_DELAY
        )

        assert response.status_code == 200
        response_set_cookie = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in response_set_cookie, (
            "Refresh must have issued Set-Cookie before CSRF preservation check"
        )

        match = re.search(rf"{SESSION_COOKIE_NAME}=([^;]+)", response_set_cookie)
        assert match, f"Could not parse refreshed cookie from: {response_set_cookie}"
        new_cookie_val = match.group(1)

        serializer = URLSafeTimedSerializer(secret_key)
        new_data = serializer.loads(new_cookie_val, salt=_SALT)

        assert new_data["csrf_token"] == original_csrf, (
            f"CSRF token must be preserved across refresh. "
            f"Expected {original_csrf!r}, got {new_data['csrf_token']!r}"
        )
