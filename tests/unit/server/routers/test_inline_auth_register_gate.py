"""Hotfix: POST /auth/register is gated by web_security.self_registration_enabled.

Before this fix any unauthenticated caller could create a live normal_user
account with no way to disable it. The gate:

- defaults CLOSED (403) and denies BEFORE any user lookup or creation,
- is read at REQUEST time from the runtime config service (a Web UI toggle
  takes effect without rebuilding the app / restarting),
- fails CLOSED when the runtime config cannot be resolved,
- leaves the enabled path byte-identical (same generic success message).

The route is registered on a bare FastAPI app via register_auth_routes()
with a fake user manager injected; the config service is a real
ConfigService backed by a temp directory, installed via set_config_service.
"""

from typing import List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.auth_error_handler import auth_error_handler
from code_indexer.server.routers.inline_auth import register_auth_routes
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)

_REGISTRATION_BODY = {
    "username": "newuser",
    "email": "newuser@example.invalid",
    "password": "Str0ng!Passw0rd#2026",
}


class RecordingUserManager:
    """Fake user manager recording every lookup and creation call."""

    def __init__(self, existing: Optional[set] = None) -> None:
        self._existing = set(existing or ())
        self.get_user_calls: List[str] = []
        self.create_user_calls: List[Tuple[str, str, object]] = []

    def get_user(self, username):
        self.get_user_calls.append(username)
        return object() if username in self._existing else None

    def create_user(self, username, password, role):
        self.create_user_calls.append((username, password, role))
        self._existing.add(username)
        return object()


class _Unused:
    """Placeholder for dependencies the register route never touches."""


@pytest.fixture
def config_svc(tmp_path):
    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    set_config_service(svc)
    yield svc
    reset_config_service()


@pytest.fixture
def user_manager():
    return RecordingUserManager()


@pytest.fixture
def client(user_manager):
    app = FastAPI()
    register_auth_routes(
        app,
        jwt_manager=_Unused(),
        user_manager=user_manager,
        refresh_token_manager=_Unused(),
    )
    return TestClient(app)


def _expected_generic_success_message() -> str:
    return str(
        auth_error_handler.create_registration_response(
            email=_REGISTRATION_BODY["email"], account_exists=False
        )["message"]
    )


class TestRegistrationDisabledByDefault:
    def test_returns_403_by_default(self, client, config_svc, user_manager):
        resp = client.post("/auth/register", json=_REGISTRATION_BODY)
        assert resp.status_code == 403
        assert "registration" in resp.json()["detail"].lower()

    def test_never_looks_up_or_creates_user_when_disabled(
        self, client, config_svc, user_manager
    ):
        client.post("/auth/register", json=_REGISTRATION_BODY)
        assert user_manager.create_user_calls == []
        assert user_manager.get_user_calls == []

    def test_explicitly_disabled_returns_403(self, client, config_svc, user_manager):
        config_svc.update_setting("web_security", "self_registration_enabled", "false")
        resp = client.post("/auth/register", json=_REGISTRATION_BODY)
        assert resp.status_code == 403
        assert user_manager.create_user_calls == []

    def test_disabled_does_not_run_timing_prevention_wrapper(
        self, client, config_svc, user_manager, monkeypatch
    ):
        calls = []

        def _recording_execute(operation):
            calls.append(operation)
            return operation()

        monkeypatch.setattr(
            auth_error_handler.timing_prevention,
            "constant_time_execute",
            _recording_execute,
        )
        resp = client.post("/auth/register", json=_REGISTRATION_BODY)
        assert resp.status_code == 403
        assert calls == []


class TestRegistrationEnabled:
    def test_creates_account_and_returns_generic_success(
        self, client, config_svc, user_manager
    ):
        from code_indexer.server.auth.user_manager import UserRole

        config_svc.update_setting("web_security", "self_registration_enabled", "true")
        resp = client.post("/auth/register", json=_REGISTRATION_BODY)

        assert resp.status_code == 200
        assert resp.json() == {"message": _expected_generic_success_message()}
        assert user_manager.create_user_calls == [
            (
                _REGISTRATION_BODY["username"],
                _REGISTRATION_BODY["password"],
                UserRole.NORMAL_USER,
            )
        ]

    def test_existing_account_gets_same_generic_success_without_create(
        self, config_svc
    ):
        existing_um = RecordingUserManager(existing={_REGISTRATION_BODY["username"]})
        app = FastAPI()
        register_auth_routes(
            app,
            jwt_manager=_Unused(),
            user_manager=existing_um,
            refresh_token_manager=_Unused(),
        )
        config_svc.update_setting("web_security", "self_registration_enabled", "true")

        resp = TestClient(app).post("/auth/register", json=_REGISTRATION_BODY)

        assert resp.status_code == 200
        assert resp.json() == {"message": _expected_generic_success_message()}
        assert existing_um.create_user_calls == []


class TestRuntimeToggle:
    def test_toggle_takes_effect_without_rebuilding_app(
        self, client, config_svc, user_manager
    ):
        assert client.post("/auth/register", json=_REGISTRATION_BODY).status_code == 403

        config_svc.update_setting("web_security", "self_registration_enabled", "true")
        assert client.post("/auth/register", json=_REGISTRATION_BODY).status_code == 200
        assert len(user_manager.create_user_calls) == 1

        config_svc.update_setting("web_security", "self_registration_enabled", "false")
        body = dict(_REGISTRATION_BODY, username="seconduser")
        assert client.post("/auth/register", json=body).status_code == 403
        assert len(user_manager.create_user_calls) == 1


class TestFailClosed:
    def test_config_service_raising_denies(self, client, user_manager, monkeypatch):
        class _BrokenConfigService:
            def get_config(self):
                raise RuntimeError("config store unavailable")

        set_config_service(_BrokenConfigService())  # type: ignore[arg-type]
        try:
            resp = client.post("/auth/register", json=_REGISTRATION_BODY)
        finally:
            reset_config_service()
        assert resp.status_code == 403
        assert user_manager.create_user_calls == []
        assert user_manager.get_user_calls == []

    def test_missing_web_security_config_denies(self, client, user_manager):
        class _NoWebSecurity:
            web_security_config = None

        class _ConfigServiceWithoutWebSecurity:
            def get_config(self):
                return _NoWebSecurity()

        set_config_service(_ConfigServiceWithoutWebSecurity())  # type: ignore[arg-type]
        try:
            resp = client.post("/auth/register", json=_REGISTRATION_BODY)
        finally:
            reset_config_service()
        assert resp.status_code == 403
        assert user_manager.create_user_calls == []
