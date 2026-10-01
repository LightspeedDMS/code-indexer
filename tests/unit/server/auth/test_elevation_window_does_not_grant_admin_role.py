"""
A normal user's own, valid, full-scope elevation window authenticates the
caller -- it never authorizes an admin-only action. Elevation and
authorization are separate checks: opening a window (now available to
every TOTP-enrolled user) proves who is calling; the role/permission gate
on each admin-only route or tool decides whether that caller may act.

Covers, through the real front door in both protocols:

- REST: a normal user opens their own elevation window via the real
  POST /auth/elevate, then POST /api/admin/users still returns 403
  "Admin access required" (a role denial, distinct from elevation_required)
  -- proven with a mutation test that neutralizes the role check.
- MCP: the same normal user's own window is denied by role on
  manage_mcp_credential (target_user set) and on create_user, through the
  real `handle_tools_call` dispatcher -- each proven with its own mutation
  test showing the identical call succeeds once that specific gate is
  neutralized.
"""

# Bind the module-level lock directory to the process data dir before
# per-test data dirs are set.
import code_indexer.server.auth.concurrency_protection  # noqa: F401

import json
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
import code_indexer.server.mcp.handlers.admin.mcp_credentials as mcp_credentials_handlers

# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------
_ELEVATE_ENFORCEMENT_PATH = (
    "code_indexer.server.auth.elevation_routes._is_elevation_enforcement_enabled"
)
_ELEVATE_TOTP_PATH = "code_indexer.server.auth.elevation_routes.get_totp_service"
_MCP_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_MCP_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_MCP_ESM_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
)

_WEB_SESSION_COOKIE_VALUE = "opaque-session-cookie-elevation-not-authorization"
_IP = "127.0.0.1"
_ELEVATION_KEY = "mcp-session-key-elevation-not-authorization"
_STRONG_PASSWORD = "Xk9$vLp2Qz#8Ymw5Tr!"


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
        username="not-an-admin-user",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def admin_username() -> str:
    return "unrelated-admin-user"


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
    """Defense in depth: unconditionally clear app.dependency_overrides
    after every test in this file (this file does not currently use
    overrides, but every sibling elevation test file does, and a future
    addition here should not have to remember this)."""
    yield
    from code_indexer.server.app import app as _app

    _app.dependency_overrides.clear()


def _stub_web_session(monkeypatch, user: User, cookie_value: str):
    """Stub the Web UI session lookup + user store so the REAL auth
    dependency chain (get_current_user_hybrid / get_current_admin_user_hybrid)
    runs unmodified for a caller presenting the "session" cookie.

    Uses monkeypatch.setattr (not a bare assignment restored by a
    hand-rolled fixture) so the ALREADY-WIRED dependencies.user_manager is
    correctly restored on teardown: a fixture that snapshots it at
    fixture-setup time -- before the first _get_app() call in the whole
    test session has run create_app() -- would capture the pre-wiring
    value (None) and overwrite the real singleton with it afterward,
    breaking every later test in the process that depends on
    dependencies.user_manager being wired.
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


def _elevate_route_ctx(enforcement: bool, mfa_enabled: bool, esm):
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    fake_totp_service.verify_enabled_code.return_value = True
    return (
        patch(_ELEVATE_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_ELEVATE_TOTP_PATH, return_value=fake_totp_service),
        patch(
            "code_indexer.server.auth.elevation_routes.elevated_session_manager",
            esm,
        ),
    )


# ===========================================================================
# (a) REST: own elevation window is insufficient for an admin-only route.
# ===========================================================================


class TestRestOwnWindowDeniedByRole:
    def test_normal_user_elevated_via_real_route_still_denied_admin_route(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        """The normal user opens a real elevation window for themselves via
        POST /auth/elevate, then POST /api/admin/users must still return
        403 "Admin access required" -- a role denial, never
        elevation_required, proving the window did not open the door."""
        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)

        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(app, raise_server_exceptions=False)
            p1, p2, p3 = _elevate_route_ctx(
                enforcement=True, mfa_enabled=True, esm=elevation_manager
            )
            with p1, p2, p3:
                elevate_response = client.post(
                    "/auth/elevate",
                    json={"totp_code": "123456"},
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )
            assert elevate_response.status_code == 200, elevate_response.text
            assert elevate_response.json()["elevated"] is True

            admin_response = client.post(
                "/api/admin/users",
                json={
                    "username": "irrelevant-target",
                    "password": _STRONG_PASSWORD,
                    "role": "normal_user",
                },
                cookies={"session": _WEB_SESSION_COOKIE_VALUE},
            )

        assert admin_response.status_code == 403, admin_response.text
        assert admin_response.json()["detail"] == "Admin access required"

    def test_mutation_neutralising_the_role_check_lets_the_same_request_succeed(
        self, tmpdir_path, elevation_manager, normal_user, monkeypatch
    ):
        """Mutation proof: with User.has_permission patched to a no-op that
        always grants, the EXACT same normal-user request that was denied
        above now succeeds -- proving the prior denial discriminated on
        that specific role check, not on unrelated plumbing."""
        from tests.unit.server.routers.inline_routes_test_helpers import (
            _find_route_handler,
            _patch_closure,
        )

        app = _get_app(tmpdir_path)
        monkeypatch.setattr(_deps, "elevated_session_manager", elevation_manager)
        elevation_manager.create(
            _WEB_SESSION_COOKIE_VALUE, normal_user.username, _IP, "full"
        )

        mock_um = MagicMock()
        mock_um.create_user_audited.return_value = User(
            username="irrelevant-target",
            password_hash="hashed",
            role=UserRole.NORMAL_USER,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        handler = _find_route_handler("/api/admin/users", "POST")

        with _stub_web_session(monkeypatch, normal_user, _WEB_SESSION_COOKIE_VALUE):
            client = TestClient(app, raise_server_exceptions=False)
            with (
                patch.object(User, "has_permission", return_value=True),
                patch(_ELEVATE_ENFORCEMENT_PATH, return_value=True),
                _patch_closure(handler, "user_manager", mock_um),
            ):
                response = client.post(
                    "/api/admin/users",
                    json={
                        "username": "irrelevant-target",
                        "password": _STRONG_PASSWORD,
                        "role": "normal_user",
                    },
                    cookies={"session": _WEB_SESSION_COOKIE_VALUE},
                )

        assert response.status_code == 201, (
            "Mutation check failed: with the role check neutralised, the "
            f"normal user's own window should now succeed, got {response.status_code}: "
            f"{response.text}"
        )


# ===========================================================================
# (b) MCP: own elevation window is insufficient for admin-only tools.
# ===========================================================================


def _parse_mcp_response(response: dict) -> dict:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


def _mcp_dispatch_ctx(enforcement: bool, esm: ElevatedSessionManager):
    """Patch the real MCP dispatcher's own request-plumbing seams (kill
    switch, TOTP service, elevation manager, session registry, repository
    access guard, telemetry) so `handle_tools_call` runs unmodified against
    the REAL TOOL_REGISTRY / HANDLER_REGISTRY entries for the tool under
    test -- mirrors the established _real_dispatch harness pattern."""
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = True

    mock_session_state = MagicMock()
    mock_session_state.is_impersonating = False

    return (
        patch("code_indexer.server.mcp.session_registry.get_session_registry"),
        patch("code_indexer.server.mcp.protocol._check_repository_access"),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch("code_indexer.server.mcp.protocol.api_metrics_service"),
        patch(_MCP_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_MCP_ESM_PATH, esm),
        patch(_MCP_TOTP_PATH, return_value=fake_totp_service),
        mock_session_state,
    )


async def _dispatch(tool_name: str, arguments: dict, user: User) -> dict:
    from code_indexer.server.mcp.protocol import handle_tools_call

    return await handle_tools_call(  # type: ignore[no-any-return]
        params={"name": tool_name, "arguments": dict(arguments)},
        user=user,
        session_id="mcp-session-id-elevation-not-authorization",
        elevation_key=_ELEVATION_KEY,
    )


@pytest.mark.asyncio
class TestMcpOwnWindowDeniedByRole:
    async def test_manage_mcp_credential_target_user_denied_by_role(
        self, elevation_manager, normal_user, admin_username
    ):
        """The normal user's own full-scope window satisfies elevation, but
        manage_mcp_credential(action=create, target_user=<someone else>)
        is still denied -- the admin-role gate in the handler body. The
        credential manager is stubbed and asserted un-called: if the role
        check ever regressed, this would otherwise write a real credential
        instead of merely returning the wrong success flag."""
        elevation_manager.create(_ELEVATION_KEY, normal_user.username, _IP, "full")

        mock_cred_manager = MagicMock()

        (
            p_reg,
            p_access,
            p_lf,
            p_metrics,
            p_enf,
            p_esm,
            p_totp,
            mock_session_state,
        ) = _mcp_dispatch_ctx(True, elevation_manager)
        with (
            patch.object(
                mcp_credentials_handlers.dependencies,
                "mcp_credential_manager",
                mock_cred_manager,
            ),
            p_reg as mock_registry,
            p_access,
            p_lf,
            p_metrics,
            p_enf,
            p_esm,
            p_totp,
        ):
            mock_registry.return_value.get_or_create_session.return_value = (
                mock_session_state
            )
            result = await _dispatch(
                "manage_mcp_credential",
                {"action": "create", "target_user": admin_username},
                normal_user,
            )

        content = _parse_mcp_response(result)
        assert content["success"] is False
        assert content["error"] == "Permission denied: admin role required"
        mock_cred_manager.generate_credential_audited.assert_not_called()
        mock_cred_manager.revoke_credential_audited.assert_not_called()

    async def test_mutation_neutralising_admin_role_check_lets_it_succeed(
        self, elevation_manager, normal_user, admin_username
    ):
        """Mutation proof for manage_mcp_credential: with _require_admin_role
        patched to a no-op, the identical call now succeeds."""
        elevation_manager.create(_ELEVATION_KEY, normal_user.username, _IP, "full")

        mock_cred_manager = MagicMock()
        mock_cred_manager.generate_credential_audited.return_value = {
            "credential_id": "cred-mutation",
            "client_id": "mcp_client_mutation",
            "client_secret": "mcp_secret_mutation",
            "name": "",
            "created_at": "2025-01-01T00:00:00Z",
        }

        (
            p_reg,
            p_access,
            p_lf,
            p_metrics,
            p_enf,
            p_esm,
            p_totp,
            mock_session_state,
        ) = _mcp_dispatch_ctx(True, elevation_manager)
        with (
            patch.object(
                mcp_credentials_handlers, "_require_admin_role", return_value=None
            ),
            patch.object(
                mcp_credentials_handlers.dependencies,
                "mcp_credential_manager",
                mock_cred_manager,
            ),
            p_reg as mock_registry,
            p_access,
            p_lf,
            p_metrics,
            p_enf,
            p_esm,
            p_totp,
        ):
            mock_registry.return_value.get_or_create_session.return_value = (
                mock_session_state
            )
            result = await _dispatch(
                "manage_mcp_credential",
                {"action": "create", "target_user": admin_username},
                normal_user,
            )

        content = _parse_mcp_response(result)
        assert content["success"] is True, (
            f"Mutation check failed: expected success once the role check "
            f"is neutralised, got {content}"
        )
        mock_cred_manager.generate_credential_audited.assert_called_once_with(
            admin_username, "", actor=normal_user.username
        )

    async def test_create_user_tool_denied_by_role(
        self, elevation_manager, normal_user
    ):
        """The normal user's own full-scope window satisfies elevation, but
        the create_user tool is still denied -- its required_permission
        (manage_users) gate at the real dispatch layer, checked before the
        handler (and its elevation decorator) ever run."""
        elevation_manager.create(_ELEVATION_KEY, normal_user.username, _IP, "full")

        (
            p_reg,
            p_access,
            p_lf,
            p_metrics,
            p_enf,
            p_esm,
            p_totp,
            mock_session_state,
        ) = _mcp_dispatch_ctx(True, elevation_manager)
        with p_reg as mock_registry, p_access, p_lf, p_metrics, p_enf, p_esm, p_totp:
            mock_registry.return_value.get_or_create_session.return_value = (
                mock_session_state
            )
            with pytest.raises(ValueError) as exc_info:
                await _dispatch(
                    "create_user",
                    {
                        "username": "irrelevant-target",
                        "password": _STRONG_PASSWORD,
                        "role": "normal_user",
                    },
                    normal_user,
                )

        assert "Permission denied" in str(exc_info.value)
        assert "manage_users" in str(exc_info.value)

    async def test_mutation_neutralising_manage_users_permission_lets_it_succeed(
        self, elevation_manager, normal_user
    ):
        """Mutation proof for create_user: with User.has_permission patched
        to a no-op that always grants, the identical call now succeeds."""
        elevation_manager.create(_ELEVATION_KEY, normal_user.username, _IP, "full")

        mock_user_manager = MagicMock()
        mock_user_manager.create_user_audited.return_value = User(
            username="irrelevant-target",
            password_hash="hashed",
            role=UserRole.NORMAL_USER,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )

        (
            p_reg,
            p_access,
            p_lf,
            p_metrics,
            p_enf,
            p_esm,
            p_totp,
            mock_session_state,
        ) = _mcp_dispatch_ctx(True, elevation_manager)
        with (
            patch.object(User, "has_permission", return_value=True),
            patch("code_indexer.server.mcp.handlers.admin._utils") as mock_utils,
            p_reg as mock_registry,
            p_access,
            p_lf,
            p_metrics,
            p_enf,
            p_esm,
            p_totp,
        ):
            mock_utils.app_module.user_manager = mock_user_manager
            mock_registry.return_value.get_or_create_session.return_value = (
                mock_session_state
            )
            result = await _dispatch(
                "create_user",
                {
                    "username": "irrelevant-target",
                    "password": _STRONG_PASSWORD,
                    "role": "normal_user",
                },
                normal_user,
            )

        content = _parse_mcp_response(result)
        assert content["success"] is True, (
            f"Mutation check failed: expected success once the permission "
            f"check is neutralised, got {content}"
        )
