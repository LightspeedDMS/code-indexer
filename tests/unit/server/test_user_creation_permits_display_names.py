"""
Front-door coverage: account-creation-time username validation
(validate_username_path_safe, wired via CreateUserRequest /
RegistrationRequest Pydantic field_validators) must accept real-world
display-style usernames -- embedded spaces, accented/non-Latin letters,
apostrophes -- and reject only path hazards.

Mirrors the existing front-door/model characterization tests
(test_front_door_username_validation.py, test_models_auth_username_validation.py) and
asserts ACCEPTANCE of these names through:
- CreateUserRequest / RegistrationRequest Pydantic models directly
- POST /auth/register (self-service registration front door)
- POST /api/admin/users (admin REST create front door)

Foundation #1 compliant: real Pydantic models, real FastAPI routes via
TestClient. Only UserManager is stubbed/recorded (heavy, unrelated
collaborator whose own path-safety coverage lives in
test_user_manager_username_path_safety.py).
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_admin_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.models.auth import CreateUserRequest, RegistrationRequest
from code_indexer.server.routers.inline_admin_users import register_admin_user_routes
from code_indexer.server.routers.inline_auth import register_auth_routes
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)


DISPLAY_STYLE_USERNAMES = [
    "john smith",
    "josé",
    "o'brien",
]


class TestCreateUserRequestAcceptsDisplayStyleUsernames:
    @pytest.mark.parametrize("username", DISPLAY_STYLE_USERNAMES)
    def test_model_accepts_username(self, username):
        req = CreateUserRequest(
            username=username, password="SecurePass123!@#", role="normal_user"
        )
        assert req.username == username


class TestRegistrationRequestAcceptsDisplayStyleUsernames:
    @pytest.mark.parametrize("username", DISPLAY_STYLE_USERNAMES)
    def test_model_accepts_username(self, username):
        req = RegistrationRequest(
            username=username,
            email="someone@example.com",
            password="SecurePass123!@#",
        )
        assert req.username == username


class _RecordingUserManager:
    def __init__(self):
        self.create_user_calls = []

    def get_user(self, username):
        return None

    def create_user(self, username, password, role):
        self.create_user_calls.append((username, password, role))
        return User(
            username=username,
            password_hash="x",
            role=role if isinstance(role, UserRole) else UserRole.NORMAL_USER,
            created_at=datetime.now(timezone.utc),
        )


class _Unused:
    """Placeholder for dependencies the register route never touches."""


class TestRegisterFrontDoorAcceptsDisplayStyleUsername:
    """POST /auth/register must accept a display-style username end to
    end -- reaching UserManager.create_user, not stopped at 422."""

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

    @pytest.mark.parametrize("username", DISPLAY_STYLE_USERNAMES)
    def test_display_style_username_reaches_create_user(self, tmp_path, username):
        user_manager = _RecordingUserManager()
        client, svc = self._client(user_manager, tmp_path)
        try:
            svc.update_setting("web_security", "self_registration_enabled", "true")
            resp = client.post(
                "/auth/register",
                json={
                    "username": username,
                    "email": "someone@example.com",
                    "password": "SecurePass123!@#",
                },
            )
            assert resp.status_code != 422, resp.text
            assert user_manager.create_user_calls
            assert user_manager.create_user_calls[0][0] == username
        finally:
            reset_config_service()


class _StubUserManager:
    def __init__(self):
        self.create_user_calls = []

    def create_user(self, username, password, role):
        self.create_user_calls.append((username, password, role))
        return User(
            username=username,
            password_hash="x",
            role=role,
            created_at=datetime.now(timezone.utc),
        )


class TestAdminRestCreateFrontDoorAcceptsDisplayStyleUsername:
    """POST /api/admin/users must accept a display-style username end to
    end -- HTTP 201, not a 4xx Pydantic rejection."""

    @pytest.mark.parametrize("username", DISPLAY_STYLE_USERNAMES)
    def test_display_style_username_returns_201(self, username):
        app = FastAPI()
        mock_admin = User(
            username="admin",
            password_hash="x",
            role=UserRole.ADMIN,
            created_at=datetime.now(timezone.utc),
        )
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: mock_admin
        stub_user_manager = _StubUserManager()
        register_admin_user_routes(
            app,
            jwt_manager=None,
            user_manager=stub_user_manager,
            refresh_token_manager=None,
            db_path_str="unused",
        )
        client = TestClient(app)

        resp = client.post(
            "/api/admin/users",
            json={
                "username": username,
                "password": "SecurePass123!@#",
                "role": "normal_user",
            },
        )

        assert resp.status_code == 201, resp.text
        assert stub_user_manager.create_user_calls
        assert stub_user_manager.create_user_calls[0][0] == username
