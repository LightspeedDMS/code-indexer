"""
Discriminating tests for admin role and elevation on global and git settings.

PUT /global/config and PUT /api/settings/git mutate server-wide configuration
(golden-repo refresh interval; fallback git committer email used for pushes
made on other users' behalf), so they require the admin role and an active
elevation window, not merely an authenticated user (`Depends(get_current_user)`
/ `Depends(get_current_user_web_or_api)`).

This mirrors the MCP twin `set_global_config`
(`server/mcp/handlers/admin/__init__.py`), which is `@require_mcp_elevation()`.

Required coverage:
- normal (non-admin) user -> 403
- admin user with NO active elevation window -> 403 elevation_required
- admin user WITH an active elevation window -> 200 (success path)

Each test overrides BOTH the bare-auth dependency (`get_current_user`
/ `get_current_user_web_or_api`) and the admin dependency
`get_current_admin_user_hybrid` (wired via `require_elevation()`). This makes
the RED phase fail for the right reason: a route gated by bare
authentication alone would accept the request, rather than 500ing on an
unrelated missing-JWT-manager setup gap. With the admin/elevation chain in
place, only that chain governs the outcome.
`get_global_repo_operations` / `_get_config_manager` are
mocked in EVERY test (not just the success path) so a currently-unrelated
`golden_repos_dir` RuntimeError never masks the auth signal, and no test ever
touches the real on-disk golden-repos config or `.code-indexer/config.json`.

Each ElevatedSessionManager is given its own isolated temp-file db_path:
the default path (`~/.cidx-server/elevated_sessions.db`) is a REAL, shared,
persistent SQLite file, so two ElevatedSessionManager() instances across
different tests would otherwise see each other's rows -- an elevation window
created by one test's "succeeds" case would leak into a later test's "no
window" case using the same session key.
"""

import contextlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import code_indexer.server.web.auth as web_auth
from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.dependencies import (
    get_current_admin_user_hybrid,
    get_current_user,
    get_current_user_web_or_api,
)
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.global_routes import git_settings as git_settings_module
from code_indexer.server.global_routes import routes as global_routes_module
from code_indexer.server.global_routes.git_settings import router as git_settings_router
from code_indexer.server.global_routes.routes import router as global_router

_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_GET_TOTP_SERVICE_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"
_SESSION_KEY = "test-session-jti-global-config-elevation"
_ADMIN_USERNAME = "global-config-admin"
_NORMAL_USERNAME = "global-config-normal-user"
_IP = "127.0.0.1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_session_manager():
    """Real (throwaway) SessionManager -- _hybrid_auth_impl() unconditionally
    calls get_session_manager() before it even inspects the cookie."""
    original = web_auth._session_manager
    web_auth.init_session_manager(secret_key="test-secret-key", config=None)
    yield
    web_auth._session_manager = original


@pytest.fixture
def elevation_manager():
    """ElevatedSessionManager backed by an isolated temp-file DB.

    The default db_path is a real, shared, persistent SQLite file
    (~/.cidx-server/elevated_sessions.db) -- using it here would let an
    elevation window created by one test leak into a later test that
    re-uses the same session key.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "elevated_sessions.db")
        yield ElevatedSessionManager(
            idle_timeout_seconds=300, max_age_seconds=1800, db_path=db_path
        )


@pytest.fixture(autouse=True)
def _restore_elevated_session_manager():
    original = getattr(_deps, "elevated_session_manager", None)
    yield
    # Typing-only cast: restore exactly what was there before the test.
    _deps.elevated_session_manager = cast(ElevatedSessionManager, original)


@pytest.fixture
def admin_user() -> User:
    return User(
        username=_ADMIN_USERNAME,
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def normal_user() -> User:
    return User(
        username=_NORMAL_USERNAME,
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@contextlib.contextmanager
def _elevation_ctx(enforcement: bool = True, mfa_enabled: bool = True):
    """Patch the elevation kill switch and TOTP service (mirrors the
    established pattern in test_admin_users_elevation_required.py)."""
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_GET_TOTP_SERVICE_PATH, return_value=fake_totp_service),
    ):
        yield


def _app_with_global_config_router() -> FastAPI:
    app = FastAPI()
    app.include_router(global_router)
    return app


def _app_with_git_settings_router() -> FastAPI:
    app = FastAPI()
    app.include_router(git_settings_router, prefix="/api")
    return app


def _cookies() -> dict:
    return {"cidx_session": _SESSION_KEY}


def _non_admin_rejection():
    """Stand-in for get_current_admin_user_hybrid's real behavior when the
    caller is authenticated but lacks the admin role -- HTTPException(403,
    'Admin access required'), exactly as get_current_admin_user() raises."""
    from fastapi import HTTPException, status

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required"
    )


def _fake_git_config_manager():
    """MagicMock ConfigManager whose load()/save() never touch real disk."""
    fake_config = MagicMock()
    fake_config.git_service.service_committer_name = "cidx-service"
    fake_config.git_service.service_committer_email = "svc@example.com"
    fake_config.git_service.default_committer_email = None
    manager = MagicMock()
    manager.load.return_value = fake_config
    return manager, fake_config


# ---------------------------------------------------------------------------
# PUT /global/config
# ---------------------------------------------------------------------------


class TestUpdateGlobalConfigElevation:
    def test_non_admin_user_is_rejected(self, isolated_session_manager, normal_user):
        app = _app_with_global_config_router()
        # Bare authentication: a normal user here must not be enough on
        # its own (bare authentication alone would accept).
        app.dependency_overrides[get_current_user] = lambda: normal_user
        # The admin dependency the route actually requires.
        app.dependency_overrides[get_current_admin_user_hybrid] = _non_admin_rejection
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(
            global_routes_module,
            "get_global_repo_operations",
            return_value=MagicMock(),
        ):
            response = client.put("/global/config", json={"refresh_interval": 120})

        assert response.status_code == 403

    def test_admin_without_elevation_window_is_refused(
        self, isolated_session_manager, admin_user, elevation_manager
    ):
        app = _app_with_global_config_router()
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        _deps.elevated_session_manager = elevation_manager
        client = TestClient(app, raise_server_exceptions=False)

        with (
            _elevation_ctx(enforcement=True),
            patch.object(
                global_routes_module,
                "get_global_repo_operations",
                return_value=MagicMock(),
            ),
        ):
            response = client.put(
                "/global/config",
                json={"refresh_interval": 120},
                cookies=_cookies(),
            )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"

    def test_admin_with_elevation_window_succeeds(
        self, isolated_session_manager, admin_user, elevation_manager
    ):
        app = _app_with_global_config_router()
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        _deps.elevated_session_manager = elevation_manager
        elevation_manager.create(_SESSION_KEY, _ADMIN_USERNAME, _IP)
        client = TestClient(app, raise_server_exceptions=False)

        mock_ops = MagicMock()
        with (
            _elevation_ctx(enforcement=True),
            patch.object(
                global_routes_module,
                "get_global_repo_operations",
                return_value=mock_ops,
            ),
        ):
            response = client.put(
                "/global/config",
                json={"refresh_interval": 120},
                cookies=_cookies(),
            )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "updated"
        mock_ops.set_config.assert_called_once_with(120, actor=_ADMIN_USERNAME)


# ---------------------------------------------------------------------------
# PUT /api/settings/git
# ---------------------------------------------------------------------------


class TestUpdateGitSettingsElevation:
    def test_non_admin_user_is_rejected(self, isolated_session_manager, normal_user):
        app = _app_with_git_settings_router()
        app.dependency_overrides[get_current_user_web_or_api] = lambda: normal_user
        app.dependency_overrides[get_current_admin_user_hybrid] = _non_admin_rejection
        client = TestClient(app, raise_server_exceptions=False)

        manager, _ = _fake_git_config_manager()
        with patch.object(
            git_settings_module, "_get_config_manager", return_value=manager
        ):
            response = client.put(
                "/api/settings/git",
                json={"default_committer_email": "new@example.com"},
            )

        assert response.status_code == 403
        manager.save.assert_not_called()

    def test_admin_without_elevation_window_is_refused(
        self, isolated_session_manager, admin_user, elevation_manager
    ):
        app = _app_with_git_settings_router()
        app.dependency_overrides[get_current_user_web_or_api] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        _deps.elevated_session_manager = elevation_manager
        client = TestClient(app, raise_server_exceptions=False)

        manager, _ = _fake_git_config_manager()
        with (
            _elevation_ctx(enforcement=True),
            patch.object(
                git_settings_module, "_get_config_manager", return_value=manager
            ),
        ):
            response = client.put(
                "/api/settings/git",
                json={"default_committer_email": "new@example.com"},
                cookies=_cookies(),
            )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"
        manager.save.assert_not_called()

    def test_admin_with_elevation_window_succeeds(
        self, isolated_session_manager, admin_user, elevation_manager
    ):
        app = _app_with_git_settings_router()
        app.dependency_overrides[get_current_user_web_or_api] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        _deps.elevated_session_manager = elevation_manager
        elevation_manager.create(_SESSION_KEY, _ADMIN_USERNAME, _IP)
        client = TestClient(app, raise_server_exceptions=False)

        manager, fake_config = _fake_git_config_manager()
        with (
            _elevation_ctx(enforcement=True),
            patch.object(
                git_settings_module, "_get_config_manager", return_value=manager
            ),
        ):
            response = client.put(
                "/api/settings/git",
                json={"default_committer_email": "new@example.com"},
                cookies=_cookies(),
            )

        assert response.status_code == 200, response.text
        manager.save.assert_called_once()
        assert fake_config.git_service.default_committer_email == "new@example.com"
