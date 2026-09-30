"""
TOTP step-up elevation is available to every TOTP-enrolled user, not only
admins: a normal user must be able to open an elevation window for
THEMSELVES (via the REST /auth/elevate endpoint and the Web UI /admin/elevate
page) and then use it against a self-service gate, exactly like an admin
already can. Only the ability to OPEN a window widens here -- admin-only
ACTIONS (e.g. the admin MCP-credential routes) keep requiring the admin role
via require_elevation(), unaffected by this file.

Covers, all through the real REST front door (TestClient + the real app):

- (a) With elevation enforcement OFF, self-service MCP-credential
  create/delete behave exactly as before enforcement existed, for both a
  normal user and an admin -- no window, no TOTP setup, needed.
- (b) With enforcement ON, a caller with no TOTP enrolled gets
  totp_setup_required with a setup_url appropriate to THEIR OWN role:
  a normal user is pointed at the self-service setup page, never the
  admin-only one.
- (c) With enforcement ON, a TOTP-enrolled normal user can open their own
  elevation window via POST /auth/elevate and then succeed at the
  self-service MCP-credential gate.
- The elevation window created through a real Web UI session (the "session"
  cookie, resolved via get_current_user_web_or_api) is found by the
  self-service gate -- the session key it was created under is the SAME key
  the gate resolves, not a different cookie name.
- A window opened by one user is never usable by another user on the
  self-service gate, even when both authenticate through the same real
  Web UI session mechanism (the cross-user invariant proven for the admin
  gate elsewhere applies here too).
- POST /auth/elevate itself no longer requires the admin role: a
  TOTP-enrolled normal user can open a window there directly.

Module-global patching uses `monkeypatch.setattr` exclusively (never a bare
`_deps.x = y` assignment restored by a hand-rolled fixture): monkeypatch
captures the CURRENT value lazily, at the point `setattr` is actually
called (after `_get_app()` has already wired the real singletons), and
restores exactly that on teardown. A fixture that snapshots
`dependencies.user_manager`/`dependencies.elevated_session_manager` at
fixture-setup time -- before the first `_get_app()` call in the whole test
session has run `create_app()` -- captures the pre-wiring value (`None` for
user_manager) and then overwrites the real singleton with it after this
file's last test, breaking every later test in the process that depends on
`dependencies.user_manager` being wired.
"""

# Bind the module-level lock directory to the process data dir before
# per-test data dirs are set.
import code_indexer.server.auth.concurrency_protection  # noqa: F401

import contextlib
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole

# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------
_SELF_SERVICE_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_SELF_SERVICE_TOTP_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"
_ELEVATE_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.elevation_routes._is_elevation_enforcement_enabled"
)
_ELEVATE_TOTP_PATH = "code_indexer.server.auth.elevation_routes.get_totp_service"

_SESSION_KEY = "test-session-jti-self-service-open-to-all"
_WEB_SESSION_COOKIE_VALUE = "opaque-web-session-cookie-self-service-open-to-all"
_IP = "127.0.0.1"
_ADMIN_SETUP_URL = "/admin/mfa/setup"
_USER_SETUP_URL = "/user/mfa/setup"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _get_app(tmpdir: str):
    """Lazy-import the real app with an isolated DB (the established
    pattern used across this test suite's elevation-gate coverage)."""
    from code_indexer.server.services.config_service import reset_config_service

    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir}):
        reset_config_service()
        from code_indexer.server.app import app as _app

        return _app


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def normal_user() -> User:
    return User(
        username="open-to-all-normal-user",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def other_normal_user() -> User:
    return User(
        username="open-to-all-other-normal-user",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def admin_user() -> User:
    return User(
        username="open-to-all-admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def elevation_manager():
    """Isolated temp-file-backed ElevatedSessionManager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "elevated_sessions.db")
        yield ElevatedSessionManager(
            idle_timeout_seconds=300, max_age_seconds=1800, db_path=db_path
        )


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    """Backstop for _self_service_client's override cleanup: unconditionally
    clear app.dependency_overrides after every test in this file, even if a
    test fails before its generator-based cleanup runs."""
    yield
    from code_indexer.server.app import app as _app

    _app.dependency_overrides.clear()


def _self_service_ctx(enforcement: bool = True, mfa_enabled: bool = True):
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    return (
        patch(_SELF_SERVICE_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_SELF_SERVICE_TOTP_PATH, return_value=fake_totp_service),
    )


@contextlib.contextmanager
def _elevate_route_ctx(
    enforcement: bool = True, mfa_enabled: bool = True, code_valid: bool = True
):
    """Patch POST /auth/elevate's own module-level seams.

    elevation_routes.py imports `elevated_session_manager` directly at
    module load time (not looked up dynamically through
    dependencies.elevated_session_manager), so a test that reassigns the
    latter must also patch elevation_routes' own binding -- otherwise
    /auth/elevate keeps talking to whatever manager was live at import time
    instead of this test's isolated temp-file-backed one.
    """
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    fake_totp_service.verify_enabled_code.return_value = code_valid
    with (
        patch(_ELEVATE_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_ELEVATE_TOTP_PATH, return_value=fake_totp_service),
        patch(
            "code_indexer.server.auth.elevation_routes.elevated_session_manager",
            _deps.elevated_session_manager,
        ),
    ):
        yield


def _fake_mcp_credential_manager():
    instance = MagicMock()
    instance.generate_credential_audited.return_value = {
        "client_id": "mcp_client_open_to_all",
        "client_secret": "mcp_secret_open_to_all",
        "credential_id": "cred-open-to-all-123",
        "name": "open-to-all-cred",
        "created_at": "2025-01-01T00:00:00Z",
    }
    instance.revoke_credential_audited.return_value = True
    return instance


def _self_service_client(app, user):
    app.dependency_overrides[_deps.get_current_user_web_or_api] = lambda: user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(_deps.get_current_user_web_or_api, None)


def _stub_web_session(monkeypatch, user: User, cookie_value: str):
    """Stub the Web UI session lookup + user store so the REAL
    get_current_user_web_or_api runs unmodified for a caller presenting
    the "session" cookie with value `cookie_value`.

    Uses monkeypatch.setattr (not a bare assignment) so the ALREADY-WIRED
    dependencies.user_manager is correctly restored on teardown, regardless
    of whether this is the first test in the process to trigger app
    creation.
    """
    from code_indexer.server.web.auth import SessionData

    fake_session_manager = MagicMock()
    fake_session_manager.get_session.return_value = SessionData(
        username=user.username,
        role=user.role.value,
        csrf_token="test-csrf-token",
        created_at=time.time(),
    )
    stub_user_manager = MagicMock()
    stub_user_manager.get_user.return_value = user
    monkeypatch.setattr(_deps, "user_manager", stub_user_manager)
    return patch(
        "code_indexer.server.web.auth.get_session_manager",
        return_value=fake_session_manager,
    )


# ---------------------------------------------------------------------------
# (a) Enforcement OFF: self-service behaves exactly as before, for everyone.
# ---------------------------------------------------------------------------


class TestEnforcementOffBehavesAsBefore:
    @pytest.mark.parametrize("role_fixture", ["normal_user", "admin_user"])
    def test_create_succeeds_without_any_window_or_totp(
        self, tmpdir_path, elevation_manager, role_fixture, request, monkeypatch
    ):
        user = request.getfixturevalue(role_fixture)
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        p1, p2 = _self_service_ctx(enforcement=False)

        for client in _self_service_client(app, user):
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                response = client.post(
                    "/api/mcp-credentials", json={"name": "no-enforcement-cred"}
                )

        assert response.status_code == 201, response.text

    @pytest.mark.parametrize("role_fixture", ["normal_user", "admin_user"])
    def test_delete_succeeds_without_any_window_or_totp(
        self, tmpdir_path, elevation_manager, role_fixture, request, monkeypatch
    ):
        user = request.getfixturevalue(role_fixture)
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        p1, p2 = _self_service_ctx(enforcement=False)

        for client in _self_service_client(app, user):
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                response = client.delete("/api/mcp-credentials/some-cred-id")

        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# (b) Enforcement ON, no TOTP enrolled: setup_url matches the caller's own
# role -- a normal user is never pointed at the admin-only setup page.
# ---------------------------------------------------------------------------


class TestTotpSetupRequiredUsesRoleAppropriateUrl:
    def test_self_service_gate_points_normal_user_at_self_service_setup(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        p1, p2 = _self_service_ctx(enforcement=True, mfa_enabled=False)

        for client in _self_service_client(app, normal_user):
            with p1, p2:
                response = client.post(
                    "/api/mcp-credentials", json={"name": "irrelevant"}
                )

        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "totp_setup_required"
        assert detail["setup_url"] == _USER_SETUP_URL

    def test_self_service_gate_still_points_admin_at_admin_setup(
        self, tmpdir_path, elevation_manager, admin_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        p1, p2 = _self_service_ctx(enforcement=True, mfa_enabled=False)

        for client in _self_service_client(app, admin_user):
            with p1, p2:
                response = client.post(
                    "/api/mcp-credentials", json={"name": "irrelevant"}
                )

        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "totp_setup_required"
        assert detail["setup_url"] == _ADMIN_SETUP_URL

    def test_auth_elevate_points_normal_user_at_self_service_setup(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(app, raise_server_exceptions=False)
            with _elevate_route_ctx(enforcement=True, mfa_enabled=False):
                response = client.post(
                    "/auth/elevate",
                    json={"totp_code": "123456"},
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )

        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "totp_setup_required"
        assert detail["setup_url"] == _USER_SETUP_URL


# ---------------------------------------------------------------------------
# (c) Enforcement ON, TOTP enrolled: a normal user opens their own window
# via the real POST /auth/elevate and then succeeds at the self-service gate.
# ---------------------------------------------------------------------------


class TestNormalUserOpensOwnWindowAndSucceeds:
    def test_auth_elevate_accepts_a_totp_enrolled_normal_user(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        """POST /auth/elevate is no longer admin-only: a TOTP-enrolled
        normal user opens a window for themselves through the real route."""
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(app, raise_server_exceptions=False)
            with _elevate_route_ctx(
                enforcement=True, mfa_enabled=True, code_valid=True
            ):
                response = client.post(
                    "/auth/elevate",
                    json={"totp_code": "123456"},
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )

        assert response.status_code == 200, response.text
        assert response.json()["elevated"] is True

    def test_normal_user_elevates_then_self_service_create_succeeds(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        """Full round trip through two real front doors: POST /auth/elevate
        opens the window, then POST /api/mcp-credentials (self-service,
        get_current_user_web_or_api) consumes it -- proving the window
        created under the real Web UI session key is the SAME key the
        self-service gate resolves."""
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)

        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(app, raise_server_exceptions=False)
            with _elevate_route_ctx(
                enforcement=True, mfa_enabled=True, code_valid=True
            ):
                elevate_response = client.post(
                    "/auth/elevate",
                    json={"totp_code": "123456"},
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )
            assert elevate_response.status_code == 200, elevate_response.text

            p1, p2 = _self_service_ctx(enforcement=True, mfa_enabled=True)
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                create_response = client.post(
                    "/api/mcp-credentials",
                    json={"name": "round-trip-cred"},
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )

        assert create_response.status_code == 201, create_response.text
        assert create_response.json()["client_id"] == "mcp_client_open_to_all"


# ---------------------------------------------------------------------------
# Web-session key binding: the self-service gate must find a window opened
# under the real "session" cookie value, and never accept one opened for a
# different user under the same mechanism.
# ---------------------------------------------------------------------------


class TestWebSessionKeyBindingOnSelfServiceGate:
    def test_self_service_create_succeeds_for_a_real_web_session_caller(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        elevation_manager.create(
            _WEB_SESSION_COOKIE_VALUE, normal_user.username, _IP, "full"
        )

        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(app, raise_server_exceptions=False)
            p1, p2 = _self_service_ctx(enforcement=True, mfa_enabled=True)
            with (
                p1,
                p2,
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=_fake_mcp_credential_manager(),
                ),
            ):
                response = client.post(
                    "/api/mcp-credentials",
                    json={"name": "web-session-cred"},
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )

        assert response.status_code == 201, response.text

    def test_self_service_denies_a_window_opened_by_a_different_user(
        self,
        tmpdir_path,
        elevation_manager,
        normal_user,
        other_normal_user,
        monkeypatch,
    ):
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        # Window belongs to other_normal_user; the request authenticates as
        # normal_user under the SAME session-key mechanism.
        elevation_manager.create(
            _WEB_SESSION_COOKIE_VALUE, other_normal_user.username, _IP, "full"
        )

        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(app, raise_server_exceptions=False)
            p1, p2 = _self_service_ctx(enforcement=True, mfa_enabled=True)
            with p1, p2:
                response = client.post(
                    "/api/mcp-credentials",
                    json={"name": "cross-user-cred"},
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"


# ---------------------------------------------------------------------------
# The Web UI /admin/elevate page itself is no longer admin-only: a
# TOTP-enrolled normal user can reach the form, and one without TOTP is
# redirected to the self-service setup page, never the admin-only one.
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _elevate_page_ctx(mfa_enabled: bool = True):
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    with patch(
        "code_indexer.server.web.elevation_web_routes.get_totp_service",
        return_value=fake_totp_service,
    ):
        yield


class TestAdminElevatePageOpenToNormalUsers:
    def test_totp_enrolled_normal_user_reaches_the_elevation_form(
        self, tmpdir_path, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(
                app, raise_server_exceptions=False, follow_redirects=False
            )
            with _elevate_page_ctx(mfa_enabled=True):
                response = client.get(
                    "/admin/elevate", cookies={"session": _WEB_SESSION_COOKIE_VALUE}
                )

        assert response.status_code == 200, response.text
        assert "totp_code" in response.text

    def test_normal_user_without_totp_is_redirected_to_self_service_setup(
        self, tmpdir_path, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(
                app, raise_server_exceptions=False, follow_redirects=False
            )
            with _elevate_page_ctx(mfa_enabled=False):
                response = client.get(
                    "/admin/elevate?next=/user/api-keys",
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )

        assert response.status_code == 303, response.text
        assert _USER_SETUP_URL in response.headers["location"]
        assert _ADMIN_SETUP_URL not in response.headers["location"]
