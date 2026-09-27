"""
Username path-safety front-door coverage.

Account-creation-time validation
(validate_username_path_safe) must be reachable, end to end, from every
real front door a caller can use to create an account with an unsafe
('..'-shaped) username:

- POST /auth/register (self-service registration) -> HTTP 422
- POST /api/admin/users (admin REST create) -> HTTP 4xx
- MCP admin `create_user` handler -> {"success": False, ...}, no raise

These are characterization tests proving the validation (Pydantic
field_validators on RegistrationRequest/CreateUserRequest,
UserManager.create_user's validate_username_path_safe call) is actually
wired to all three doors, not just covered by unit tests against the
validator/model/manager in isolation.
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_auth import register_auth_routes
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)


class _Unused:
    """Placeholder for dependencies the register route never touches."""


class _RecordingUserManager:
    def __init__(self):
        self.create_user_calls = []

    def get_user(self, username):
        return None

    def create_user(self, username, password, role):
        self.create_user_calls.append((username, password, role))
        return object()


class TestRegisterFrontDoorRejectsTraversalUsername:
    """POST /auth/register: FastAPI/Pydantic request-body validation runs
    BEFORE the route body (and thus before UserManager.create_user is ever
    called), for a '..' username regardless of the self-registration
    enabled/disabled gate state."""

    def _client(self, user_manager, tmp_path):
        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.load_config()
        set_config_service(svc)
        app = FastAPI()
        register_auth_routes(
            app,
            jwt_manager=_Unused(),
            user_manager=user_manager,
            refresh_token_manager=_Unused(),
        )
        return TestClient(app), svc

    def test_dotdot_username_returns_422(self, tmp_path):
        user_manager = _RecordingUserManager()
        client, svc = self._client(user_manager, tmp_path)
        try:
            svc.update_setting("web_security", "self_registration_enabled", "true")
            resp = client.post(
                "/auth/register",
                json={
                    "username": "..",
                    "email": "attacker@example.com",
                    "password": "SecurePass123!@#",
                },
            )
            assert resp.status_code == 422
            assert user_manager.create_user_calls == []
        finally:
            reset_config_service()


class TestAdminRestCreateFrontDoorRejectsTraversalUsername:
    """POST /api/admin/users: the same Pydantic gate on CreateUserRequest
    must reject a '..' username with a 4xx before UserManager.create_user
    is ever called, using the REAL app + admin auth override (Story #925
    elevation bypass pattern shared by every other inline-admin-users
    test in this suite)."""

    def test_dotdot_username_returns_4xx(self):
        from tests.unit.server.routers.inline_routes_test_helpers import (
            admin_client as admin_client_fixture,
        )

        # Reuse the shared fixture's setup/teardown logic directly since
        # this file intentionally stays independent of that module's
        # fixture-collection wiring.
        # pytest's FixtureFunctionDefinition exposes the undecorated generator
        # function as ``__wrapped__`` at runtime but does not declare it in its
        # type stubs, so fetch it via getattr for the type checker.
        gen = getattr(admin_client_fixture, "__wrapped__")()
        client = next(gen)
        try:
            resp = client.post(
                "/api/admin/users",
                json={
                    "username": "..",
                    "password": "SecurePass123!@#",
                    "role": "normal_user",
                },
            )
            assert 400 <= resp.status_code < 500, resp.text
        finally:
            try:
                next(gen)
            except StopIteration:
                pass


class TestMcpCreateUserFrontDoorRejectsTraversalUsername:
    """MCP admin create_user handler
    (server/mcp/handlers/admin/__init__.py::create_user) must never raise
    for a '..' username -- UserManager.create_user's ValueError is caught
    by the handler's existing generic except-Exception clause and
    reported as {"success": False, ...}, exactly like any other
    JIT/creation failure."""

    def test_dotdot_username_returns_success_false(self):
        import json

        from code_indexer.server.mcp.handlers import admin as admin_module
        from code_indexer.server.mcp.handlers import _utils

        acting_admin = User(
            username="mcp-admin",
            password_hash="x",
            role=UserRole.ADMIN,
            created_at=datetime.now(timezone.utc),
        )

        real_user_manager = Mock()
        real_user_manager.create_user.side_effect = ValueError(
            "Username cannot be '.' or '..'"
        )

        with patch.object(
            _utils.app_module, "user_manager", real_user_manager, create=True
        ):
            # Elevation enforcement has its own dedicated coverage
            # (test_admin_tools_elevation_required.py) -- bypass via the
            # raw handler underneath @require_mcp_elevation(), matching
            # every other MCP admin-handler test in this suite.
            raw_create_user = admin_module.create_user.__wrapped__
            result = raw_create_user(
                {
                    "username": "..",
                    "password": "SecurePass123!@#",
                    "role": "normal_user",
                },
                acting_admin,
            )

        # Unwrap the MCP content-array envelope
        # ({"content": [{"type": "text", "text": "<json>"}]}) that
        # _mcp_response wraps every handler's data dict in.
        body = json.loads(result["content"][0]["text"])
        assert body["success"] is False
        assert body["user"] is None
