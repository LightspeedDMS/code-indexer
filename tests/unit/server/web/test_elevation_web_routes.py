"""
TestClient-based unit tests for GET /admin/elevate and POST /auth/elevate-form
(Story #923 AC7).

All external dependencies (admin auth, TOTP service, elevation session manager,
kill-switch) are patched with MagicMock — no real DB, no live server.
"""

import contextlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.web.elevation_web_routes import router as elevation_web_router

# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.web.elevation_web_routes._is_elevation_enforcement_enabled"
)
_TOTP_SERVICE_PATH = "code_indexer.server.web.elevation_web_routes.get_totp_service"
_ESM_PATH = "code_indexer.server.web.elevation_web_routes.elevated_session_manager"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_COOKIE = "test-session-key-abc"
_USERNAME = "admin"
# Dummy bcrypt-format string — not a real credential; avoids hash computation.
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    """Minimal admin User with a dummy (non-functional) password hash."""
    from datetime import datetime, timezone

    from code_indexer.server.auth.user_manager import User

    return User(
        username=_USERNAME,
        role="admin",
        password_hash=_DUMMY_HASH,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def app(admin_user):
    """Minimal FastAPI app with the elevation_web_router and admin auth overridden."""
    _app = FastAPI()
    # Mount the templates directory so TemplateResponse resolves correctly.
    # The router uses os.path.dirname(__file__) which resolves at import time.
    _app.include_router(elevation_web_router)
    from code_indexer.server.auth import dependencies as _deps

    _app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    return _app


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=True, follow_redirects=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_totp(
    mfa_enabled: bool = True, totp_ok: bool = True, recovery_ok: bool = True
):
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = mfa_enabled
    svc.verify_enabled_code.return_value = totp_ok
    svc.verify_recovery_code.return_value = recovery_ok
    return svc


def _fake_esm():
    esm = MagicMock()
    esm.create.return_value = None
    return esm


@contextlib.contextmanager
def _ctx(enforcement: bool = True, totp_svc=None, esm=None):
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
# Tests — GET /admin/elevate
# ---------------------------------------------------------------------------


def test_elevate_page_renders_form_when_mfa_enabled(client):
    """Admin with TOTP configured gets 200 with form HTML."""
    totp_svc = _fake_totp(mfa_enabled=True)
    with _ctx(totp_svc=totp_svc):
        resp = client.get("/admin/elevate")
    # Templates render as HTML; we check that the response is successful.
    # A 200 means the template was rendered (template file must exist).
    assert resp.status_code == 200
    assert "elevateForm" in resp.text or "totp_code" in resp.text


def test_elevate_page_redirects_to_setup_when_mfa_disabled(client):
    """Admin without TOTP configured gets 303 redirect to /admin/mfa/setup."""
    totp_svc = _fake_totp(mfa_enabled=False)
    with _ctx(totp_svc=totp_svc):
        resp = client.get("/admin/elevate?next=/admin/some-page")
    assert resp.status_code == 303
    assert "/admin/mfa/setup" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Tests — POST /auth/elevate-form
# ---------------------------------------------------------------------------


def test_elevate_form_redirects_to_next_on_valid_totp(client):
    """POST valid TOTP code creates elevation window and redirects to next."""
    esm = _fake_esm()
    totp_svc = _fake_totp(mfa_enabled=True, totp_ok=True)
    with _ctx(totp_svc=totp_svc, esm=esm):
        resp = client.post(
            "/auth/elevate-form",
            data={"totp_code": "123456", "next": "/admin/"},
            cookies={"cidx_session": _SESSION_COOKIE},
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/"
    esm.create.assert_called_once()
    call_kwargs = esm.create.call_args.kwargs
    assert call_kwargs["session_key"] == _SESSION_COOKIE
    assert call_kwargs["username"] == _USERNAME
    assert call_kwargs["scope"] == "full"


def test_elevate_form_returns_401_on_invalid_totp(client):
    """POST invalid TOTP code returns 401 and does NOT create elevation window."""
    esm = _fake_esm()
    totp_svc = _fake_totp(mfa_enabled=True, totp_ok=False)
    with _ctx(totp_svc=totp_svc, esm=esm):
        resp = client.post(
            "/auth/elevate-form",
            data={"totp_code": "000000", "next": "/admin/"},
            cookies={"cidx_session": _SESSION_COOKIE},
        )
    assert resp.status_code == 401
    esm.create.assert_not_called()


def test_elevate_form_handles_recovery_code(client):
    """POST valid recovery code creates elevation window with scope=totp_repair."""
    esm = _fake_esm()
    totp_svc = _fake_totp(mfa_enabled=True, recovery_ok=True)
    with _ctx(totp_svc=totp_svc, esm=esm):
        resp = client.post(
            "/auth/elevate-form",
            data={"recovery_code": "AAAA-BBBB-CCCC-DDDD", "next": "/admin/"},
            cookies={"cidx_session": _SESSION_COOKIE},
        )
    assert resp.status_code == 303
    esm.create.assert_called_once()
    call_kwargs = esm.create.call_args.kwargs
    assert call_kwargs["scope"] == "totp_repair"


def test_elevate_form_503_when_kill_switch_off(client):
    """Kill switch disabled returns 503."""
    with _ctx(enforcement=False):
        resp = client.post(
            "/auth/elevate-form",
            data={"totp_code": "123456", "next": "/admin/"},
            cookies={"cidx_session": _SESSION_COOKIE},
        )
    assert resp.status_code == 503
