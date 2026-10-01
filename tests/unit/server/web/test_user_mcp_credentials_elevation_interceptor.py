"""
The self-service MCP-credential page (user_mcp_credentials.html, extending
user_base.html) must show the same inline TOTP elevation prompt admin pages
already show on a 403 elevation_required/totp_setup_required response,
instead of a bare "Failed to generate MCP credential" message with no path
forward.

base.html and user_base.html share ONE elevation-interceptor script
(static/js/elevation_interceptor.js) rather than duplicating the ~250-line
inline logic; each page sets its own role-appropriate fallback values
(window._cidxTotpSetupFallbackUrl, window._cidxFormInterceptPrefix) before
loading it. base.html's own values are unchanged.

Covers:
- The real /user/mcp-credentials page (TestClient, real app) includes the
  shared interceptor script and the elevation modal markup, with
  /user/mfa/setup (the self-service setup page every role can already
  reach) as its TOTP-setup fallback -- never the admin-only page.
- The real /admin/ dashboard page still includes the same shared script,
  with its own unchanged /admin/mfa/setup fallback -- proving base.html's
  behavior is untouched.
- The real POST /api/mcp-credentials front door, under enforcement with no
  elevation window, returns the exact JSON shape
  ({"detail": {"error": "elevation_required"}}) the shared script's
  `body.detail.error` check parses.
- A static contract check: the shared script still contains the literal
  error-code strings it must match against server responses.
"""

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

_SELF_SERVICE_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_SELF_SERVICE_TOTP_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"
_WEB_SESSION_COOKIE_VALUE = "opaque-session-cookie-elevation-interceptor"

_INTERCEPTOR_SCRIPT_SRC = "/admin/static/js/elevation_interceptor.js"
_USER_SETUP_URL = "/user/mfa/setup"
_ADMIN_SETUP_URL = "/admin/mfa/setup"


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
        username="interceptor-normal-user",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def admin_user() -> User:
    return User(
        username="interceptor-admin-user",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def elevation_manager():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "elevated_sessions.db")
        yield ElevatedSessionManager(
            idle_timeout_seconds=300, max_age_seconds=1800, db_path=db_path
        )


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    yield
    from code_indexer.server.app import app as _app

    _app.dependency_overrides.clear()


def _stub_routes_session(monkeypatch, user: User, cookie_value: str):
    """Stub the web-session lookup exactly where `routes.py`'s own
    `_require_authenticated_session` looks it up (a name imported into
    `routes.py`'s namespace at module load, not looked up dynamically), and
    the user store, so the real page-rendering code path runs unmodified."""
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
    stub_user_manager.get_mcp_credentials.return_value = []
    monkeypatch.setattr(_deps, "user_manager", stub_user_manager)
    monkeypatch.setattr(
        "code_indexer.server.web.routes.get_session_manager",
        lambda: fake_session_manager,
    )


class TestUserMcpCredentialsPageHasElevationInterceptor:
    def test_user_page_includes_shared_interceptor_script(
        self, tmpdir_path, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        _stub_routes_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get(
            "/user/mcp-credentials", cookies={"session": _WEB_SESSION_COOKIE_VALUE}
        )

        assert response.status_code == 200, response.text
        assert _INTERCEPTOR_SCRIPT_SRC in response.text

    def test_user_page_includes_the_elevation_modal_markup(
        self, tmpdir_path, normal_user, monkeypatch
    ):
        app = _get_app(tmpdir_path)
        _stub_routes_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get(
            "/user/mcp-credentials", cookies={"session": _WEB_SESSION_COOKIE_VALUE}
        )

        assert response.status_code == 200, response.text
        assert 'id="elevationModal"' in response.text
        assert 'id="elevationTotpCode"' in response.text
        assert 'id="elevationVerifyBtn"' in response.text

    def test_user_page_points_the_fallback_at_the_self_service_setup_page(
        self, tmpdir_path, normal_user, monkeypatch
    ):
        """The TOTP-setup fallback on a /user/ page must be the self-service
        setup page every role can reach -- never the admin-only one."""
        app = _get_app(tmpdir_path)
        _stub_routes_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get(
            "/user/mcp-credentials", cookies={"session": _WEB_SESSION_COOKIE_VALUE}
        )

        assert response.status_code == 200, response.text
        assert f"_cidxTotpSetupFallbackUrl = '{_USER_SETUP_URL}'" in response.text
        assert _ADMIN_SETUP_URL not in response.text


class TestAdminPageBehaviourUnchanged:
    def test_admin_login_page_still_points_the_fallback_at_the_admin_setup_page(
        self, tmpdir_path
    ):
        """Regression guard: base.html's own fallback value is untouched by
        extracting the shared script -- /admin/ pages still default to the
        admin-only setup page."""
        app = _get_app(tmpdir_path)
        client = TestClient(app, raise_server_exceptions=False)

        # /admin/ redirects to /login when unauthenticated; the login page
        # itself extends base.html (via the shared login template chain)
        # and is reachable with no stubbing at all.
        response = client.get("/login", follow_redirects=False)

        assert response.status_code == 200, response.text
        assert _INTERCEPTOR_SCRIPT_SRC in response.text
        assert f"_cidxTotpSetupFallbackUrl = '{_ADMIN_SETUP_URL}'" in response.text
        assert f"_cidxFormInterceptPrefix = '/admin/'" in response.text  # noqa: F541


class TestElevationRequiredResponseShapeMatchesTheScriptsContract:
    def test_self_service_create_without_a_window_returns_the_parsed_shape(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        """The real front door's elevation_required response is exactly the
        shape the shared script's `body.detail.error` check expects."""
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        app.dependency_overrides[_deps.get_current_user_web_or_api] = (
            lambda: normal_user
        )
        client = TestClient(app, raise_server_exceptions=False)

        fake_totp_service = MagicMock()
        fake_totp_service.is_mfa_enabled.return_value = True
        with (
            patch(_SELF_SERVICE_ENFORCEMENT_PATH, return_value=True),
            patch(_SELF_SERVICE_TOTP_PATH, return_value=fake_totp_service),
        ):
            response = client.post(
                "/api/mcp-credentials", json={"name": "interceptor-contract-cred"}
            )

        assert response.status_code == 403, response.text
        assert response.json() == {"detail": {"error": "elevation_required"}}

    def test_shared_script_still_matches_the_real_error_codes(self):
        from code_indexer.server.auth.dependencies import (
            _ERROR_ELEVATION_REQUIRED,
            _ERROR_TOTP_SETUP_REQUIRED,
        )

        script_path = (
            Path(__file__).parents[4]
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "static"
            / "js"
            / "elevation_interceptor.js"
        )
        script_text = script_path.read_text()

        assert f"'{_ERROR_ELEVATION_REQUIRED}'" in script_text
        assert f"'{_ERROR_TOTP_SETUP_REQUIRED}'" in script_text
