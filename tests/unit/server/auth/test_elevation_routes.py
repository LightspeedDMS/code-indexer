"""
TestClient-based unit tests for POST /auth/elevate and GET /auth/elevation-status
(Story #923 AC3+AC4).

All external dependencies (admin auth, TOTP service, elevation session manager,
kill-switch) are patched with MagicMock — no real DB, no live server.
"""

import contextlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.elevated_session_manager import ElevatedSession
from code_indexer.server.auth.elevation_routes import router as elevation_router

# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.elevation_routes._is_elevation_enforcement_enabled"
)
_TOTP_SERVICE_PATH = "code_indexer.server.auth.elevation_routes.get_totp_service"
_ESM_PATH = "code_indexer.server.auth.elevation_routes.elevated_session_manager"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_JTI = "test-jti-abc123"
_USERNAME = "admin"
_NOW = 1_700_000_000.0
_IDLE_TIMEOUT = 300
_MAX_AGE = 1800
# Dummy bcrypt-format string — not a real credential; avoids hash computation.
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    """Minimal admin User with a dummy (non-functional) password hash."""
    from code_indexer.server.auth.user_manager import User

    return User(
        username=_USERNAME,
        role="admin",
        password_hash=_DUMMY_HASH,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def app(admin_user):
    """Minimal FastAPI app with the elevation router and admin auth overridden."""
    _app = FastAPI()
    _app.include_router(elevation_router)
    from code_indexer.server.auth import dependencies as _deps

    _app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    return _app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fake_session(scope: str = "full") -> ElevatedSession:
    return ElevatedSession(
        session_key=_SESSION_JTI,
        username=_USERNAME,
        elevated_at=_NOW,
        last_touched_at=_NOW,
        elevated_from_ip="127.0.0.1",
        scope=scope,
    )


def _fake_esm(scope: str = "full") -> MagicMock:
    esm = MagicMock()
    esm._idle_timeout = _IDLE_TIMEOUT
    esm._max_age = _MAX_AGE
    esm.get_status.return_value = _fake_session(scope)
    esm.create.return_value = None
    return esm


def _fake_totp(
    mfa_enabled: bool = True,
    totp_ok: bool = True,
    recovery_ok: bool = True,
) -> MagicMock:
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = mfa_enabled
    svc.verify_enabled_code.return_value = totp_ok
    svc.verify_recovery_code.return_value = recovery_ok
    return svc


@contextlib.contextmanager
def _elevate_ctx(enforcement: bool = True, totp_svc=None, esm=None):
    """Patch kill-switch, TOTP service, and elevated_session_manager together."""
    if totp_svc is None:
        totp_svc = _fake_totp()
    if esm is None:
        esm = _fake_esm()
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_TOTP_SERVICE_PATH, return_value=totp_svc),
        patch(_ESM_PATH, esm),
    ):
        yield esm


# ---------------------------------------------------------------------------
# POST /auth/elevate — request-shape and kill-switch errors (parametrized)
#
# None of these cases send a cidx_session cookie because the error fires
# before session-key resolution (kill-switch, body shape, MFA gate) or the
# no-session-key case intentionally omits the cookie.
# ---------------------------------------------------------------------------

_ELEVATE_ERROR_CASES = [
    pytest.param(
        False,
        {"totp_code": "123456"},
        {},
        503,
        "elevation_enforcement_disabled",
        id="kill_switch_off_returns_503",
    ),
    pytest.param(
        True,
        {},
        {},
        400,
        "missing_code",
        id="missing_code_returns_400",
    ),
    pytest.param(
        True,
        {"totp_code": "123456", "recovery_code": "XXXX-XXXX-XXXX-XXXX"},
        {},
        400,
        "ambiguous_code",
        id="both_codes_returns_400",
    ),
    pytest.param(
        True,
        {"totp_code": "123456"},
        {"mfa_enabled": False},
        403,
        "totp_setup_required",
        id="no_totp_setup_returns_403",
    ),
    # mfa_enabled=True but no cidx_session cookie → session key absent → 403
    pytest.param(
        True,
        {"totp_code": "123456"},
        {"mfa_enabled": True},
        403,
        "elevation_required",
        id="no_session_key_returns_403",
    ),
]


@pytest.mark.parametrize(
    "enforcement,body,totp_kwargs,expected_status,expected_error",
    _ELEVATE_ERROR_CASES,
)
def test_elevate_error_cases(
    client, enforcement, body, totp_kwargs, expected_status, expected_error
):
    """Parametrized: all /auth/elevate error paths (no cidx_session cookie sent)."""
    totp_svc = _fake_totp(**totp_kwargs)
    with _elevate_ctx(enforcement=enforcement, totp_svc=totp_svc):
        resp = client.post("/auth/elevate", json=body)
    assert resp.status_code == expected_status
    assert resp.json()["detail"]["error"] == expected_error


# ---------------------------------------------------------------------------
# POST /auth/elevate — code-verification success and failure (parametrized)
# ---------------------------------------------------------------------------

_CODE_VERIFY_CASES = [
    pytest.param(
        {"totp_code": "000000"},
        {"totp_ok": False},
        None,
        401,
        "elevation_failed",
        None,
        id="invalid_totp_returns_401",
    ),
    pytest.param(
        {"totp_code": "123456"},
        {"totp_ok": True},
        "full",
        200,
        None,
        "full",
        id="valid_totp_creates_full_window",
    ),
    pytest.param(
        {"recovery_code": "XXXX-XXXX-XXXX-XXXX"},
        {"recovery_ok": False},
        None,
        401,
        "elevation_failed",
        None,
        id="invalid_recovery_returns_401",
    ),
    pytest.param(
        {"recovery_code": "AAAA-BBBB-CCCC-DDDD"},
        {"recovery_ok": True},
        "totp_repair",
        200,
        None,
        "totp_repair",
        id="valid_recovery_creates_repair_window",
    ),
]


@pytest.mark.parametrize(
    "body,totp_kwargs,esm_scope,expected_status,expected_error,expected_scope",
    _CODE_VERIFY_CASES,
)
def test_elevate_code_verification(
    client,
    body,
    totp_kwargs,
    esm_scope,
    expected_status,
    expected_error,
    expected_scope,
):
    """Parametrized: TOTP/recovery success and failure paths (cidx_session cookie sent)."""
    totp_svc = _fake_totp(**totp_kwargs)
    esm = _fake_esm(scope=esm_scope or "full")
    with _elevate_ctx(totp_svc=totp_svc, esm=esm):
        resp = client.post(
            "/auth/elevate",
            json=body,
            cookies={"cidx_session": _SESSION_JTI},
        )
    assert resp.status_code == expected_status
    if expected_error:
        assert resp.json()["detail"]["error"] == expected_error
    if expected_scope:
        assert resp.json()["scope"] == expected_scope
        esm.create.assert_called_once()


# ---------------------------------------------------------------------------
# GET /auth/elevation-status — "not elevated" variants (parametrized)
# ---------------------------------------------------------------------------

_STATUS_NOT_ELEVATED_CASES = [
    pytest.param(
        False,
        None,
        id="kill_switch_off_returns_not_elevated",
    ),
    pytest.param(
        True,
        None,
        id="no_session_cookie_returns_not_elevated",
    ),
]


@pytest.mark.parametrize("enforcement,session_cookie", _STATUS_NOT_ELEVATED_CASES)
def test_elevation_status_not_elevated(client, enforcement, session_cookie):
    """Parametrized: both conditions that yield {elevated: false}."""
    esm = _fake_esm()
    esm.get_status.return_value = None  # no active window
    with _elevate_ctx(enforcement=enforcement, esm=esm):
        cookies = {"cidx_session": session_cookie} if session_cookie else {}
        resp = client.get("/auth/elevation-status", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["elevated"] is False


# ---------------------------------------------------------------------------
# GET /auth/elevation-status — active window (structurally unique: touch_atomic check)
# ---------------------------------------------------------------------------


def test_elevation_status_active_window_returns_elevated_and_does_not_touch(client):
    """Active window → elevated=true with timestamps; touch_atomic MUST NOT be called."""
    esm = _fake_esm(scope="full")
    with _elevate_ctx(enforcement=True, esm=esm):
        resp = client.get(
            "/auth/elevation-status",
            cookies={"cidx_session": _SESSION_JTI},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["elevated"] is True
    assert body["scope"] == "full"
    assert body["elevated_until"] == pytest.approx(_NOW + _IDLE_TIMEOUT)
    assert body["max_until"] == pytest.approx(_NOW + _MAX_AGE)
    esm.get_status.assert_called_once_with(_SESSION_JTI)
    esm.touch_atomic.assert_not_called()


# ---------------------------------------------------------------------------
# Rate limiting on POST /auth/elevate (Codex Fix 5)
# ---------------------------------------------------------------------------


def test_elevate_rate_limit_lockout_returns_429(client):
    """After max_attempts failures, the next POST /auth/elevate returns 429.

    Uses a fresh LoginRateLimiter with max_attempts=3 patched into the route
    so the test is deterministic and fast.
    """
    from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter

    fresh_limiter = LoginRateLimiter(max_attempts=3, lockout_duration_minutes=15)
    totp_svc = _fake_totp(totp_ok=False)  # always fails verification
    esm = _fake_esm()

    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_TOTP_SERVICE_PATH, return_value=totp_svc),
        patch(_ESM_PATH, esm),
        patch(
            "code_indexer.server.auth.elevation_routes.login_rate_limiter",
            fresh_limiter,
        ),
    ):
        # Three failures — limiter records each but not yet locked after 2
        for _ in range(3):
            resp = client.post(
                "/auth/elevate",
                json={"totp_code": "000000"},
                cookies={"cidx_session": _SESSION_JTI},
            )
        # After 3 failures the account is locked; next call → 429
        resp = client.post(
            "/auth/elevate",
            json={"totp_code": "000000"},
            cookies={"cidx_session": _SESSION_JTI},
        )
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "rate_limited"


def test_elevate_success_clears_rate_counter(client):
    """A successful elevation resets the failure counter.

    Sequence: 2 failures → 1 success → 1 failure → no lockout (counter was cleared).
    """
    from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter

    fresh_limiter = LoginRateLimiter(max_attempts=3, lockout_duration_minutes=15)
    esm = _fake_esm()

    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_ESM_PATH, esm),
        patch(
            "code_indexer.server.auth.elevation_routes.login_rate_limiter",
            fresh_limiter,
        ),
    ):
        # Two failures
        totp_fail = _fake_totp(totp_ok=False)
        with patch(_TOTP_SERVICE_PATH, return_value=totp_fail):
            for _ in range(2):
                client.post(
                    "/auth/elevate",
                    json={"totp_code": "000000"},
                    cookies={"cidx_session": _SESSION_JTI},
                )

        # One success — clears counter
        totp_ok = _fake_totp(totp_ok=True)
        with patch(_TOTP_SERVICE_PATH, return_value=totp_ok):
            resp = client.post(
                "/auth/elevate",
                json={"totp_code": "123456"},
                cookies={"cidx_session": _SESSION_JTI},
            )
        assert resp.status_code == 200

        # One more failure — counter was reset so still no lockout
        with patch(_TOTP_SERVICE_PATH, return_value=totp_fail):
            resp = client.post(
                "/auth/elevate",
                json={"totp_code": "000000"},
                cookies={"cidx_session": _SESSION_JTI},
            )
        assert resp.status_code == 401  # fail, but NOT 429 (not locked)
