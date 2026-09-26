"""
Discriminating tests for TOTP elevation on the admin MCP-credential routes.

The four ADMIN MCP-credential routes in `inline_mcp_creds.py`
(create/list/revoke a target user's credential, plus the system-wide
"list all credentials" variant) mutate or disclose bearer credential
material (client_id/client_secret) equivalent to a password, so they
require TOTP elevation on top of the admin role, matching the MCP twin
(`mcp/handlers/admin/mcp_credentials.py`), which requires elevation via
`@require_mcp_elevation()` for the mutating actions.

The SELF-SERVICE, non-admin routes in the SAME file (create/delete of the
CALLER'S OWN credential) are covered separately -- see
test_inline_mcp_creds_self_service_elevation.py for their full functional
coverage. The structural assertions below capture the resulting split
between the two elevation gates in this file:

- `POST /api/mcp-credentials` (create) and
  `DELETE /api/mcp-credentials/{credential_id}` (delete) -- mutating,
  self-service -- carry an elevation dependency, but it is the SELF-SERVICE
  gate (`inline_mcp_creds._require_self_elevation`, which resolves the
  caller via `get_current_user_web_or_api` and works for ANY authenticated
  user), never this file's admin-only `require_elevation.<locals>._check`
  (which hard-requires `get_current_admin_user_hybrid` and would incorrectly
  reject every NORMAL_USER self-service caller).
- `GET /api/mcp-credentials` (list, read-only) remains completely
  unelevated -- out of scope for both gates -- and must carry NEITHER
  dependency.

Coverage:
- Structural: all 4 admin routes carry the admin require_elevation() check;
  the 2 self-service MUTATING routes carry the self-service elevation
  check instead; the 1 self-service READ route carries neither.
- Functional: admin without an active elevation window is refused
  (403 elevation_required) on every admin route; admin WITH an elevation
  window succeeds (representative: create route).
"""

import contextlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies as _deps
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"
# The self-service elevation gate is a plain module-level function (not a
# factory-produced closure like the admin one above), so its __qualname__
# is just its own name.
_SELF_SERVICE_ELEVATION_QUALNAME = "_require_self_elevation"
_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_GET_TOTP_SERVICE_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"
_SESSION_KEY = "test-session-jti-admin-mcp-creds"
_ADMIN_USERNAME = "admin-mcp-creds-admin"
_IP = "127.0.0.1"

# (path, method, expected: has require_elevation())
_ADMIN_ROUTE_CASES = [
    ("/api/admin/users/{username}/mcp-credentials", "GET", True),
    ("/api/admin/users/{username}/mcp-credentials", "POST", True),
    ("/api/admin/users/{username}/mcp-credentials/{credential_id}", "DELETE", True),
    ("/api/admin/mcp-credentials", "GET", True),
]

# Self-service MUTATING routes: must carry the SELF-SERVICE elevation gate
# specifically -- not this file's admin-only one.
_SELF_SERVICE_ELEVATED_CASES = [
    ("/api/mcp-credentials", "POST"),
    ("/api/mcp-credentials/{credential_id}", "DELETE"),
]

# Self-service READ route: read-only, out of scope for either elevation
# gate -- must carry NEITHER elevation dependency.
_SELF_SERVICE_UNELEVATED_CASES = [
    ("/api/mcp-credentials", "GET"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_has_dependency_qualname(route, qualname: str) -> bool:
    for dep in getattr(route, "dependencies", []) or []:
        dep_callable = getattr(dep, "dependency", None)
        if dep_callable is None:
            continue
        if getattr(dep_callable, "__qualname__", "") == qualname:
            return True
    return False


def _route_has_elevation_dep(route) -> bool:
    return _route_has_dependency_qualname(route, _ELEVATION_QUALNAME)


def _route_has_self_service_elevation_dep(route) -> bool:
    return _route_has_dependency_qualname(route, _SELF_SERVICE_ELEVATION_QUALNAME)


def _find_route(app, path: str, method: str):
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if route.path == path and method in (route.methods or []):
            return route
    return None


def _get_app(tmpdir: str):
    """Lazy-import the real app with an isolated DB (mirrors
    test_admin_users_elevation_required.py's _get_app helper)."""
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
def admin_user() -> User:
    return User(
        username=_ADMIN_USERNAME,
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def elevation_manager():
    """Isolated temp-file-backed ElevatedSessionManager (never the real
    shared ~/.cidx-server/elevated_sessions.db -- see the global-config elevation test
    file for the cross-test leak this avoids)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "elevated_sessions.db")
        yield ElevatedSessionManager(
            idle_timeout_seconds=300, max_age_seconds=1800, db_path=db_path
        )


@pytest.fixture(autouse=True)
def _restore_elevated_session_manager():
    original = _deps.elevated_session_manager
    yield
    _deps.elevated_session_manager = original


@contextlib.contextmanager
def _elevation_ctx(enforcement: bool = True, mfa_enabled: bool = True):
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_GET_TOTP_SERVICE_PATH, return_value=fake_totp_service),
    ):
        yield


def _admin_client(app, admin_user):
    app.dependency_overrides[_deps.get_current_admin_user] = lambda: admin_user
    app.dependency_overrides[_deps.get_current_admin_user_hybrid] = lambda: admin_user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(_deps.get_current_admin_user, None)
    app.dependency_overrides.pop(_deps.get_current_admin_user_hybrid, None)


def _fake_target_user() -> User:
    return User(
        username="someuser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Structural coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,method,expected", _ADMIN_ROUTE_CASES)
def test_admin_mcp_credential_routes_require_elevation(
    tmpdir_path, path, method, expected
):
    app = _get_app(tmpdir_path)
    route = _find_route(app, path, method)
    assert route is not None, f"{method} {path} route not found"
    assert _route_has_elevation_dep(route) is expected, (
        f"{method} {path}: expected require_elevation()={expected}"
    )


@pytest.mark.parametrize("path,method", _SELF_SERVICE_ELEVATED_CASES)
def test_self_service_mutating_routes_require_self_service_elevation(
    tmpdir_path, path, method
):
    """Self-service create/delete must carry the SELF-SERVICE elevation
    gate, never the admin-only one this file's routes use -- reusing the
    admin gate would hard-require admin role and break self-service for
    every NORMAL_USER account."""
    app = _get_app(tmpdir_path)
    route = _find_route(app, path, method)
    assert route is not None, f"{method} {path} route not found"
    assert _route_has_self_service_elevation_dep(route), (
        f"{method} {path}: expected the self-service elevation "
        "gate (_require_self_elevation)"
    )
    assert not _route_has_elevation_dep(route), (
        f"{method} {path}: must NOT carry the admin-only require_elevation() "
        "gate -- that would reject non-admin self-service callers"
    )


@pytest.mark.parametrize("path,method", _SELF_SERVICE_UNELEVATED_CASES)
def test_self_service_read_route_remains_unelevated(tmpdir_path, path, method):
    """Scope boundary: the self-service LIST route is read-only and must
    carry NEITHER elevation gate."""
    app = _get_app(tmpdir_path)
    route = _find_route(app, path, method)
    assert route is not None, f"{method} {path} route not found"
    assert not _route_has_elevation_dep(route), (
        f"{method} {path}: must NOT have the admin require_elevation() gate"
    )
    assert not _route_has_self_service_elevation_dep(route), (
        f"{method} {path}: must NOT have the self-service elevation gate "
        "(the read-only list route is explicitly out of scope for it)"
    )


# ---------------------------------------------------------------------------
# Functional coverage: refused without elevation
# ---------------------------------------------------------------------------


class TestAdminMcpCredentialElevationRefusal:
    """Each test mocks the closure-bound user_manager/MCPCredentialManager
    calls so a route without the elevation gate would reach a clean success
    response instead of an unrelated 500 from the isolated test app's
    unconfigured SQLite path -- the discriminating signal is 403 (required)
    vs. 200/201 (what an ungated route would return)."""

    def test_list_user_credentials_without_elevation_is_refused(
        self, tmpdir_path, admin_user, elevation_manager
    ):
        from tests.unit.server.routers.inline_routes_test_helpers import (
            _find_route_handler,
            _patch_closure,
        )

        app = _get_app(tmpdir_path)
        _deps.elevated_session_manager = elevation_manager
        mock_um = MagicMock()
        mock_um.get_user.return_value = _fake_target_user()
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "GET"
        )
        mock_mcp_manager_instance = MagicMock()
        mock_mcp_manager_instance.get_credentials.return_value = []

        for client in _admin_client(app, admin_user):
            with (
                _elevation_ctx(enforcement=True),
                _patch_closure(handler, "user_manager", mock_um),
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=mock_mcp_manager_instance,
                ),
            ):
                response = client.get(
                    "/api/admin/users/someuser/mcp-credentials",
                    cookies={"cidx_session": _SESSION_KEY},
                )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"

    def test_create_user_credential_without_elevation_is_refused(
        self, tmpdir_path, admin_user, elevation_manager
    ):
        from tests.unit.server.routers.inline_routes_test_helpers import (
            _find_route_handler,
            _patch_closure,
        )

        app = _get_app(tmpdir_path)
        _deps.elevated_session_manager = elevation_manager
        mock_um = MagicMock()
        mock_um.get_user.return_value = _fake_target_user()
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "POST"
        )
        mock_mcp_manager_instance = MagicMock()
        mock_mcp_manager_instance.generate_credential.return_value = {
            "credential_id": "cred-123",
            "client_id": "mcp_client_abc",
            "client_secret": "mcp_secret_xyz",
            "name": "attacker-minted",
            "created_at": "2025-01-01T00:00:00Z",
        }

        for client in _admin_client(app, admin_user):
            with (
                _elevation_ctx(enforcement=True),
                _patch_closure(handler, "user_manager", mock_um),
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=mock_mcp_manager_instance,
                ),
            ):
                response = client.post(
                    "/api/admin/users/someuser/mcp-credentials",
                    json={"name": "attacker-minted"},
                    cookies={"cidx_session": _SESSION_KEY},
                )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"

    def test_revoke_user_credential_without_elevation_is_refused(
        self, tmpdir_path, admin_user, elevation_manager
    ):
        from tests.unit.server.routers.inline_routes_test_helpers import (
            _find_route_handler,
            _patch_closure,
        )

        app = _get_app(tmpdir_path)
        _deps.elevated_session_manager = elevation_manager
        mock_um = MagicMock()
        mock_um.get_user.return_value = _fake_target_user()
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials/{credential_id}", "DELETE"
        )
        mock_mcp_manager_instance = MagicMock()
        mock_mcp_manager_instance.revoke_credential.return_value = True

        for client in _admin_client(app, admin_user):
            with (
                _elevation_ctx(enforcement=True),
                _patch_closure(handler, "user_manager", mock_um),
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=mock_mcp_manager_instance,
                ),
            ):
                response = client.delete(
                    "/api/admin/users/someuser/mcp-credentials/some-cred-id",
                    cookies={"cidx_session": _SESSION_KEY},
                )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"

    def test_list_all_credentials_without_elevation_is_refused(
        self, tmpdir_path, admin_user, elevation_manager
    ):
        from tests.unit.server.routers.inline_routes_test_helpers import (
            _find_route_handler,
            _patch_closure,
        )

        app = _get_app(tmpdir_path)
        _deps.elevated_session_manager = elevation_manager
        mock_um = MagicMock()
        mock_um.list_all_mcp_credentials.return_value = []
        handler = _find_route_handler("/api/admin/mcp-credentials", "GET")

        for client in _admin_client(app, admin_user):
            with (
                _elevation_ctx(enforcement=True),
                _patch_closure(handler, "user_manager", mock_um),
            ):
                response = client.get(
                    "/api/admin/mcp-credentials",
                    cookies={"cidx_session": _SESSION_KEY},
                )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "elevation_required"


# ---------------------------------------------------------------------------
# Functional coverage: succeeds with an active elevation window
# ---------------------------------------------------------------------------


class TestAdminMcpCredentialElevationSuccess:
    def test_create_user_credential_with_elevation_succeeds(
        self, tmpdir_path, admin_user, elevation_manager
    ):
        from tests.unit.server.routers.inline_routes_test_helpers import (
            _find_route_handler,
            _patch_closure,
        )

        app = _get_app(tmpdir_path)
        _deps.elevated_session_manager = elevation_manager
        elevation_manager.create(_SESSION_KEY, _ADMIN_USERNAME, _IP)

        mock_um = MagicMock()
        mock_um.get_user.return_value = User(
            username="targetuser",
            password_hash="hashed",
            role=UserRole.NORMAL_USER,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        handler = _find_route_handler(
            "/api/admin/users/{username}/mcp-credentials", "POST"
        )

        fake_credential = {
            "credential_id": "cred-123",
            "client_id": "mcp_client_abc",
            "client_secret": "mcp_secret_xyz",
            "name": "attacker-minted",
            "created_at": "2025-01-01T00:00:00Z",
        }
        mock_mcp_manager_instance = MagicMock()
        mock_mcp_manager_instance.generate_credential.return_value = fake_credential

        for client in _admin_client(app, admin_user):
            with (
                _elevation_ctx(enforcement=True),
                _patch_closure(handler, "user_manager", mock_um),
                patch(
                    "code_indexer.server.auth.mcp_credential_manager.MCPCredentialManager",
                    return_value=mock_mcp_manager_instance,
                ),
            ):
                response = client.post(
                    "/api/admin/users/targetuser/mcp-credentials",
                    json={"name": "attacker-minted"},
                    cookies={"cidx_session": _SESSION_KEY},
                )

        assert response.status_code == 201, response.text
        assert response.json()["client_id"] == "mcp_client_abc"
