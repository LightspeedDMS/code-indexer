"""
Tests for mfa_routes.py AC5 and AC6 (Story #925).

AC5: Cross-user TOTP setup requires (a) active elevation + (b) confirm_overwrite=1.
     Self-service setup is unchanged.
AC6: mfa_disable and mfa_recovery_codes_page require active elevation window.

12 tests:
  - AC5 cross-user overwrite guard (4 standalone)
  - AC6 disable/recovery-codes elevation gate (4, 2 parametrized pairs)
  - AC6 cross-user requires full scope (2 parametrized)
  - AC5 audit log written on cross-user success (1)
  - AC5 no mutation without elevation (1)
"""

import contextlib
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import CIDX_SESSION_COOKIE
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.web.mfa_routes import mfa_router, set_totp_service

# ---------------------------------------------------------------------------
# Patch target constants
# ---------------------------------------------------------------------------
_SESSION_USERNAME_PATH = "code_indexer.server.web.mfa_routes._get_session_username"
_ESM_PATH = "code_indexer.server.web.mfa_routes.elevated_session_manager"

# Non-sensitive username constants used as test identities.
_ADMIN = "admin_user"
_OTHER = "other_admin"
_VALID_TOTP = "123456"

# Idle / max-age for in-process ElevatedSessionManager.
_IDLE_SECONDS = 300
_MAX_AGE_SECONDS = 1800


def _make_session_key() -> str:
    """Return a unique, non-sensitive test session identifier per call."""
    return f"test-session-{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------


def _assert_elevation_required(resp) -> None:
    """Assert response is 403 with error=elevation_required."""
    assert resp.status_code == 403
    assert resp.json().get("error") == "elevation_required"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def totp_mock():
    """Mock TOTP service: MFA enabled, all operations succeed."""
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    svc.generate_secret.return_value = "JBSWY3DPEHPK3PXP"
    svc.get_provisioning_uri.return_value = "otpauth://totp/test"
    svc.generate_qr_code.return_value = b"\x89PNG\r\n"
    svc.get_manual_entry_key.return_value = "ABCD EFGH"
    svc.generate_recovery_codes.return_value = ["AAAA-1111", "BBBB-2222"]
    svc.verify_code.side_effect = lambda u, c: c == _VALID_TOTP
    svc.activate_mfa.return_value = True
    svc.disable_mfa.return_value = None
    return svc


@pytest.fixture
def esm(tmp_path):
    """Real ElevatedSessionManager backed by a temp SQLite database."""
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_SECONDS,
        max_age_seconds=_MAX_AGE_SECONDS,
        db_path=str(tmp_path / "elev.db"),
    )


@pytest.fixture
def app(totp_mock):
    """Minimal FastAPI app with mfa_router; TOTP service injected."""
    _app = FastAPI()
    set_totp_service(totp_mock)
    _app.include_router(mfa_router)
    yield _app
    set_totp_service(None)


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


# ---------------------------------------------------------------------------
# Context-manager helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _as_admin(username: str = _ADMIN):
    """Patch _get_session_username to return *username*."""
    with patch(_SESSION_USERNAME_PATH, return_value=username):
        yield


@contextlib.contextmanager
def _with_elevation(
    esm: ElevatedSessionManager, session_key: str, username: str, scope: str = "full"
):
    """Open elevation window in *esm* and patch the module-level singleton."""
    esm.create(
        session_key=session_key,
        username=username,
        elevated_from_ip=None,
        scope=scope,
    )
    with patch(_ESM_PATH, esm):
        yield


def _elevated_cookies(session_key: str) -> dict:
    """Build cookie dict using the production cookie name constant."""
    return {CIDX_SESSION_COOKIE: session_key}


# ---------------------------------------------------------------------------
# AC5: cross-user overwrite guard
# ---------------------------------------------------------------------------


def test_self_setup_no_elevation_required(client, esm):
    """Self-setup (no ?user param) succeeds without elevation."""
    with _as_admin(_ADMIN), patch(_ESM_PATH, esm):
        resp = client.get("/admin/mfa/setup")
    assert resp.status_code == 200


def test_cross_user_setup_no_elevation_returns_403(client, esm):
    """Cross-user setup without active elevation window returns 403 elevation_required."""
    with _as_admin(_ADMIN), patch(_ESM_PATH, esm):
        resp = client.get(f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1")
    _assert_elevation_required(resp)


def test_cross_user_setup_with_elevation_no_confirm_overwrite_returns_400(client, esm):
    """Cross-user setup WITH elevation but missing confirm_overwrite=1 returns 400."""
    session_key = _make_session_key()
    with _as_admin(_ADMIN), _with_elevation(esm, session_key, _ADMIN, scope="full"):
        resp = client.get(
            f"/admin/mfa/setup?user={_OTHER}",
            cookies=_elevated_cookies(session_key),
        )
    assert resp.status_code == 400
    assert resp.json().get("error") == "confirm_overwrite_required"


def test_cross_user_setup_with_elevation_and_confirm_overwrite_succeeds(client, esm):
    """Cross-user setup WITH elevation AND confirm_overwrite=1 returns 200."""
    session_key = _make_session_key()
    with _as_admin(_ADMIN), _with_elevation(esm, session_key, _ADMIN, scope="full"):
        resp = client.get(
            f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1",
            cookies=_elevated_cookies(session_key),
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# AC6: disable + recovery-codes elevation gate (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,req_kwargs",
    [
        ("post", "/admin/mfa/disable", {"data": {"totp_code": _VALID_TOTP}}),
        ("get", "/admin/mfa/recovery-codes", {}),
    ],
    ids=["disable", "recovery-codes"],
)
def test_ac6_endpoint_without_elevation_returns_403(
    client, esm, method, path, req_kwargs
):
    """Elevation-gated admin endpoints return 403 when no active window."""
    with _as_admin(_ADMIN), patch(_ESM_PATH, esm):
        resp = getattr(client, method)(path, **req_kwargs)
    _assert_elevation_required(resp)


@pytest.mark.parametrize(
    "method,path,req_kwargs,expected_status",
    [
        ("post", "/admin/mfa/disable", {"data": {"totp_code": _VALID_TOTP}}, 303),
        ("get", "/admin/mfa/recovery-codes", {}, 200),
    ],
    ids=["disable", "recovery-codes"],
)
def test_ac6_endpoint_with_totp_repair_elevation_succeeds(
    client, esm, method, path, req_kwargs, expected_status
):
    """Elevation-gated admin endpoints succeed with totp_repair-scope elevation."""
    session_key = _make_session_key()
    with (
        _as_admin(_ADMIN),
        _with_elevation(esm, session_key, _ADMIN, scope="totp_repair"),
    ):
        resp = getattr(client, method)(
            path, cookies=_elevated_cookies(session_key), **req_kwargs
        )
    assert resp.status_code == expected_status


# ---------------------------------------------------------------------------
# AC6: cross-user ops require full scope, not totp_repair (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1"),
        ("get", f"/admin/mfa/recovery-codes?user={_OTHER}"),
    ],
    ids=["cross-user-setup", "cross-user-recovery-codes"],
)
def test_ac6_cross_user_with_totp_repair_scope_returns_403(client, esm, method, path):
    """Cross-user TOTP operations with totp_repair scope return 403 (full required)."""
    session_key = _make_session_key()
    with (
        _as_admin(_ADMIN),
        _with_elevation(esm, session_key, _ADMIN, scope="totp_repair"),
    ):
        resp = getattr(client, method)(path, cookies=_elevated_cookies(session_key))
    _assert_elevation_required(resp)


# ---------------------------------------------------------------------------
# AC5: audit log written on cross-user success
# ---------------------------------------------------------------------------


def test_cross_user_setup_writes_audit_log(client, esm):
    """Successful cross-user setup writes a logger.info entry naming both admins."""
    session_key = _make_session_key()
    with _as_admin(_ADMIN), _with_elevation(esm, session_key, _ADMIN, scope="full"):
        with patch("code_indexer.server.web.mfa_routes.logger") as mock_logger:
            resp = client.get(
                f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1",
                cookies=_elevated_cookies(session_key),
            )
    assert resp.status_code == 200
    logged = [str(c) for c in mock_logger.info.call_args_list]
    assert any(_OTHER in m and _ADMIN in m for m in logged), (
        f"Expected audit log entry with {_ADMIN!r} and {_OTHER!r}; got: {logged}"
    )


# ---------------------------------------------------------------------------
# AC5: no mutation without elevation
# ---------------------------------------------------------------------------


def test_cross_user_without_elevation_does_not_call_generate_secret(
    client, totp_mock, esm
):
    """403 on cross-user setup must not have triggered generate_secret."""
    with _as_admin(_ADMIN), patch(_ESM_PATH, esm):
        resp = client.get(f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1")
    assert resp.status_code == 403
    totp_mock.generate_secret.assert_not_called()
