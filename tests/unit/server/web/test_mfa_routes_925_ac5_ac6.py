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
    """Assert response is 403 HTML error page (Bug C: guard returns HTML, not JSON)."""
    assert resp.status_code == 403
    content_type = resp.headers.get("content-type", "")
    assert "text/html" in content_type, (
        f"Expected HTML error page for elevation_required, got: {content_type}"
    )
    assert "/admin/" in resp.text, "HTML error page must contain back-link to /admin/"


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
    """Cross-user setup WITH elevation but missing confirm_overwrite=1 returns 400 HTML
    (Bug C: guard returns styled HTML error page, not raw JSON)."""
    session_key = _make_session_key()
    with _as_admin(_ADMIN), _with_elevation(esm, session_key, _ADMIN, scope="full"):
        resp = client.get(
            f"/admin/mfa/setup?user={_OTHER}",
            cookies=_elevated_cookies(session_key),
        )
    assert resp.status_code == 400
    content_type = resp.headers.get("content-type", "")
    assert "text/html" in content_type, (
        f"Expected HTML error page for confirm_overwrite_required, got: {content_type}"
    )
    assert "/admin/" in resp.text, "HTML error page must contain back-link to /admin/"


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


# ---------------------------------------------------------------------------
# Navigation escape-route: back link must go to dashboard, not /admin/users
# ---------------------------------------------------------------------------


def test_render_setup_default_back_link_is_dashboard():
    """_render_setup default back_link must be /admin/ so admins can escape setup."""
    from code_indexer.server.web.mfa_routes import _render_setup

    html = _render_setup("qrdata", "MANUALKEY", "csrf", "testuser")
    assert "href='/admin/'" in html, "back link must point to /admin/ by default"


def test_render_setup_back_link_label_contains_cancel():
    """_render_setup back link label must contain 'Cancel' so the escape is obvious."""
    from code_indexer.server.web.mfa_routes import _render_setup

    html = _render_setup("qrdata", "MANUALKEY", "csrf", "testuser")
    assert "Cancel" in html, "back link must say 'Cancel' (not just 'Back')"


def test_render_qr_error_default_back_link_is_dashboard():
    """_render_qr_error default back_link must be /admin/ (dashboard escape route)."""
    from unittest.mock import MagicMock

    from code_indexer.server.web.mfa_routes import _render_qr_error, set_totp_service

    mock_svc = MagicMock()
    mock_svc.get_provisioning_uri.return_value = "otpauth://totp/test"
    mock_svc.generate_qr_code.return_value = b"\x89PNG"
    mock_svc.get_manual_entry_key.return_value = "ABCDEFGHIJ"
    set_totp_service(mock_svc)
    try:
        resp = _render_qr_error("testuser", "bad code", show_mode=False)
        assert "href='/admin/'" in resp.body.decode(), (
            "back link in QR error page must point to /admin/ by default"
        )
    finally:
        set_totp_service(None)


# ---------------------------------------------------------------------------
# Bug fix: _resolve_session_key must prefer user_jti over cidx_session cookie
# ---------------------------------------------------------------------------


def test_resolve_session_key_prefers_user_jti_over_cidx_cookie():
    """Bug fix: _resolve_session_key must prefer request.state.user_jti over cidx_session cookie.

    Web UI auth stores elevation under the "session" cookie value (mapped via user_jti),
    not under "cidx_session". Previously mfa_routes only checked "cidx_session" causing
    elevation lookup to always fail for web UI session users."""
    from code_indexer.server.web.mfa_routes import _resolve_session_key

    request = MagicMock()
    request.state.user_jti = "web-ui-session-key"
    request.cookies.get.return_value = "cidx-session-value"  # different value

    result = _resolve_session_key(request)
    assert result == "web-ui-session-key"


def test_resolve_session_key_falls_back_to_cidx_cookie_when_no_user_jti():
    """_resolve_session_key falls back to cidx_session cookie when user_jti absent."""
    from code_indexer.server.auth.dependencies import CIDX_SESSION_COOKIE
    from code_indexer.server.web.mfa_routes import _resolve_session_key

    class _NoJtiState:
        pass

    request = MagicMock()
    request.state = _NoJtiState()  # no user_jti attribute
    request.cookies = {CIDX_SESSION_COOKIE: "cidx-session-value"}

    result = _resolve_session_key(request)
    assert result == "cidx-session-value"


def test_check_elevation_window_finds_window_via_user_jti(esm):
    """Bug fix: _check_elevation_window must find elevation stored under user_jti.

    When web UI auth sets request.state.user_jti (from the "session" cookie),
    the elevation window is stored under that jti value. _check_elevation_window
    must look up that key, not the cidx_session cookie."""
    from code_indexer.server.web.mfa_routes import _check_elevation_window

    session_key = _make_session_key()
    esm.create(
        session_key=session_key,
        username=_ADMIN,
        elevated_from_ip=None,
        scope="full",
    )

    # Build a minimal request-like object with user_jti set but no cidx_session cookie.
    request = MagicMock()
    request.state.user_jti = session_key
    request.cookies = {}  # no cidx_session cookie present

    with patch(_ESM_PATH, esm):
        result = _check_elevation_window(request, required_scope="full")

    # Before fix: returns elevation_required error dict (jti key not checked).
    # After fix: returns None (elevation found via user_jti).
    assert result is None, f"Expected None (elevation found), got: {result}"


# ---------------------------------------------------------------------------
# Bug fix: _resolve_session_key must fall back to session cookie for
# endpoints like mfa_setup_page that use _get_session_username (not
# get_current_admin_user_hybrid), so user_jti is never set on request.state.
# ---------------------------------------------------------------------------


def test_resolve_session_key_falls_back_to_session_cookie_when_no_jti_no_cidx():
    """_resolve_session_key uses session cookie when user_jti and cidx_session are absent.

    This is the SSO production scenario: mfa_setup_page does not call
    get_current_admin_user_hybrid so user_jti is not set, and SSO users have
    no cidx_session cookie. The session cookie must be the intermediate fallback."""
    from code_indexer.server.web.mfa_routes import _resolve_session_key

    class _NoJtiState:
        pass

    request = MagicMock()
    request.state = _NoJtiState()  # no user_jti attribute
    request.cookies = {"session": "web-session-id-abc"}  # only session cookie present

    result = _resolve_session_key(request)
    assert result == "web-session-id-abc"


def test_cross_user_setup_with_session_cookie_elevation_succeeds(client, esm):
    """SSO scenario: cross-user setup succeeds when elevation is stored under the
    session cookie value and no cidx_session cookie is present.

    elevate_ajax stores elevation under user_jti = session cookie value.
    mfa_setup_page has no user_jti set on request.state (it uses _get_session_username
    not get_current_admin_user_hybrid), so _resolve_session_key must fall back to
    reading the session cookie directly to find the elevation window."""
    session_cookie_value = _make_session_key()
    # Create elevation window under the session cookie value (as elevate_ajax does)
    esm.create(
        session_key=session_cookie_value,
        username=_ADMIN,
        elevated_from_ip=None,
        scope="full",
    )
    with _as_admin(_ADMIN), patch(_ESM_PATH, esm):
        resp = client.get(
            f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1",
            # Only session cookie present — no cidx_session cookie
            cookies={"session": session_cookie_value},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Bug A: cross-user show mode must not require confirm_overwrite=1
# Bug C: cross-user guard must return HTML, not raw JSON (two branches)
# Bug D: show-mode form must have required on totp_code input
# ---------------------------------------------------------------------------


def test_cross_user_show_mode_with_elevation_no_confirm_overwrite_succeeds(client, esm):
    """Bug A: mode=show is read-only; cross-user show access needs elevation only,
    NOT confirm_overwrite=1. Previously the guard fired for all cross-user access
    and blocked show mode with a 400 unless confirm_overwrite=1 was supplied."""
    session_key = _make_session_key()
    with _as_admin(_ADMIN), _with_elevation(esm, session_key, _ADMIN, scope="full"):
        resp = client.get(
            f"/admin/mfa/setup?user={_OTHER}&mode=show",
            cookies=_elevated_cookies(session_key),
        )
    assert resp.status_code == 200


def test_cross_user_show_mode_without_elevation_returns_403_html(client, esm):
    """Bug A + Bug C: cross-user show without elevation must return 403 HTML
    (not raw JSON). The error page should contain an anchor back to /admin/."""
    with _as_admin(_ADMIN), patch(_ESM_PATH, esm):
        resp = client.get(f"/admin/mfa/setup?user={_OTHER}&mode=show")
    assert resp.status_code == 403
    content_type = resp.headers.get("content-type", "")
    assert "text/html" in content_type, (
        f"Expected HTML response, got Content-Type: {content_type}"
    )
    body = resp.text
    assert "/admin/" in body, "Error page must contain back-link to /admin/"


def test_cross_user_non_show_mode_still_requires_confirm_overwrite(client, esm):
    """Bug A non-regression: non-show cross-user mode (generating new secret) still
    requires confirm_overwrite=1 even with elevation. The guard must only be relaxed
    for mode=show, not for all cross-user operations."""
    session_key = _make_session_key()
    with _as_admin(_ADMIN), _with_elevation(esm, session_key, _ADMIN, scope="full"):
        resp = client.get(
            f"/admin/mfa/setup?user={_OTHER}",  # no mode=show, no confirm_overwrite
            cookies=_elevated_cookies(session_key),
        )
    assert resp.status_code == 400


def test_cross_user_guard_elevation_required_returns_html_not_json(client, esm):
    """Bug C branch 1: _cross_user_setup_guard elevation_required error must return
    HTMLResponse, not raw JSON. Browser users should see a styled error page."""
    with _as_admin(_ADMIN), patch(_ESM_PATH, esm):
        # No elevation window — triggers elevation_required branch
        resp = client.get(f"/admin/mfa/setup?user={_OTHER}&confirm_overwrite=1")
    assert resp.status_code == 403
    content_type = resp.headers.get("content-type", "")
    assert "text/html" in content_type, (
        f"Expected HTML error page for elevation_required, got: {content_type}"
    )
    assert "/admin/" in resp.text, "HTML error page must contain back-link to /admin/"


def test_cross_user_guard_confirm_overwrite_required_returns_html_not_json(client, esm):
    """Bug C branch 2: _cross_user_setup_guard confirm_overwrite_required error must
    return HTMLResponse, not raw JSON (elevated but missing confirm_overwrite param)."""
    session_key = _make_session_key()
    with _as_admin(_ADMIN), _with_elevation(esm, session_key, _ADMIN, scope="full"):
        resp = client.get(
            f"/admin/mfa/setup?user={_OTHER}",  # no confirm_overwrite
            cookies=_elevated_cookies(session_key),
        )
    assert resp.status_code == 400
    content_type = resp.headers.get("content-type", "")
    assert "text/html" in content_type, (
        f"Expected HTML error page for confirm_overwrite_required, got: {content_type}"
    )
    assert "/admin/" in resp.text, "HTML error page must contain back-link to /admin/"


def test_show_mode_form_totp_input_has_required_attribute():
    """Bug D: the show-mode TOTP input element itself must carry the required attribute.
    Without it, submitting an empty form sends an empty string to Form(...),
    producing a 422 Unprocessable Entity from FastAPI.
    Checks the totp_code input element substring, not just any occurrence of required."""
    from code_indexer.server.web.mfa_routes import _render_setup

    html = _render_setup("qrdata", "MANUALKEY", "csrf", "testuser", show_mode=True)
    # Find the totp_code input element; it must contain 'required'
    # The input has name='totp_code' so locate that span and check for required
    totp_input_start = html.find("name='totp_code'")
    assert totp_input_start != -1, "totp_code input element not found in show_mode HTML"
    # The required attribute lives on the same input tag; find the enclosing tag
    tag_start = html.rfind("<input", 0, totp_input_start)
    tag_end = html.find(">", totp_input_start)
    assert tag_start != -1 and tag_end != -1, "Could not locate totp_code <input> tag"
    input_tag = html[tag_start : tag_end + 1]
    assert "required" in input_tag, (
        f"totp_code input tag in show_mode must include 'required'; got: {input_tag!r}"
    )
