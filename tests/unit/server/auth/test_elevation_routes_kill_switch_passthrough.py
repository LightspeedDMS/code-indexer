"""Tests for POST /auth/elevate kill-switch behavior.

When elevation enforcement is disabled (kill switch off), POST /auth/elevate
must return HTTP 503 with error 'elevation_enforcement_disabled' (NOT a synthetic
200 success). The /auth/elevate endpoint has no meaning when elevation is
administratively disabled — silent passthrough would violate anti-fallback
(Rule 2) and anti-silent-failure (Rule 13).

Patch targets follow the exact established pattern in test_elevation_routes.py:
patch at the module-level import seam (not internal helpers).
"""

import contextlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.elevated_session_manager import ElevatedSession
from code_indexer.server.auth.elevation_routes import router as elevation_router
import code_indexer.server.auth.dependencies as _deps

# ---------------------------------------------------------------------------
# Patch targets (same seams as test_elevation_routes.py)
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.elevation_routes._is_elevation_enforcement_enabled"
)
_TOTP_SERVICE_PATH = "code_indexer.server.auth.elevation_routes.get_totp_service"
_ESM_PATH = "code_indexer.server.auth.elevation_routes.elevated_session_manager"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_JTI = "test-jti-passthrough-001"
_USERNAME = "admin"
_NOW = 1_700_000_000.0
_IDLE_TIMEOUT = 300
_MAX_AGE = 1800
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_elevation_routes.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    from code_indexer.server.auth.user_manager import User

    return User(
        username=_USERNAME,
        role="admin",
        password_hash=_DUMMY_HASH,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def app(admin_user):
    _app = FastAPI()
    _app.include_router(elevation_router)
    _app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    return _app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


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


def _fake_totp(mfa_enabled: bool = True, totp_ok: bool = True) -> MagicMock:
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = mfa_enabled
    svc.verify_enabled_code.return_value = totp_ok
    return svc


@contextlib.contextmanager
def _elevate_ctx(enforcement: bool = True, totp_svc=None, esm=None):
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
# Tests
# ---------------------------------------------------------------------------


def test_post_elevate_when_disabled_returns_503(client):
    """Kill switch OFF -> HTTP 503 elevation_enforcement_disabled.

    Per CLAUDE.md: 'Kill switch returns HTTP 503 NOT 403 when
    elevation_enforcement_enabled=false. 503 correctly signals
    feature administratively off.'
    """
    with _elevate_ctx(enforcement=False):
        resp = client.post("/auth/elevate", json={"totp_code": "123456"})

    assert resp.status_code == 503, (
        f"Expected 503 (kill switch off), got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["detail"]["error"] == "elevation_enforcement_disabled", (
        f"Expected elevation_enforcement_disabled, got: {body}"
    )


def test_post_elevate_when_enabled_validates_otp_normally(client):
    """Kill switch ON + invalid OTP -> HTTP 401 (normal validation path unchanged).

    Confirms that enabling the passthru for the disabled case does NOT break
    the enforcement-on path.
    """
    totp_svc = _fake_totp(mfa_enabled=True, totp_ok=False)
    esm = _fake_esm()
    with _elevate_ctx(enforcement=True, totp_svc=totp_svc, esm=esm):
        resp = client.post(
            "/auth/elevate",
            json={"totp_code": "000000"},
            cookies={"cidx_session": _SESSION_JTI},
        )

    assert resp.status_code == 401, (
        f"Expected 401 (invalid OTP), got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["detail"]["error"] == "elevation_failed"
