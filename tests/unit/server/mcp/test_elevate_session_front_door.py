"""
Front-door tests for the MCP ``elevate_session`` tool.

Invariants under test:

1. ``elevate_session`` is a registered MCP tool: a caller authenticated with a
   login JWT (Bearer) and with TOTP enrolled submits a TOTP code over MCP
   ``tools/call`` and gets an elevation window keyed by that token's jti.
2. An elevation-gated tool returns ``elevation_required`` before the window
   exists, and proceeds once it does.
3. A wrong code opens no window (``elevation_failed``); a caller without TOTP
   enrolled gets ``totp_setup_required``.
4. A window opened with one token never satisfies the gate for another token
   of the same user.
5. Any TOTP-enrolled role may elevate, matching REST ``POST /auth/elevate``
   (which accepts any authenticated user).

Every call goes through the real ``POST /mcp`` endpoint (``mcp_router``) with
a real ``Authorization: Bearer <jwt>`` header. JWTManager, UserManager,
TOTPService, ElevatedSessionManager and LoginRateLimiter are REAL objects
backed by temporary storage. Only the elevation-enforcement config read is
pinned ON, and Langfuse/group tool-access are disabled.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from unittest.mock import patch

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server import app as real_app_module
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.elevation_routes import router as elevation_router
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.mcp.protocol import mcp_router
from code_indexer.server.web import mfa_routes

_PASSWORD = "Front-Door-Pa55word!"
_GATED_TOOL = "set_session_impersonation"
_DECORATOR = "code_indexer.server.mcp.auth.elevation_decorator"
_HANDLER = "code_indexer.server.mcp.handlers.admin.elevate_session"
_REST = "code_indexer.server.auth.elevation_routes"


class _FrontDoor:
    """Real auth stack plus a TestClient over the real MCP router."""

    def __init__(self, tmp_path: Path) -> None:
        self.jwt = JWTManager(secret_key="front-door-test-secret")
        self.users = UserManager(users_file_path=str(tmp_path / "users.json"))
        self.totp = TOTPService(
            db_path=str(tmp_path / "totp.db"),
            mfa_encryption_key=Fernet.generate_key().decode(),
        )
        self.esm = ElevatedSessionManager(
            idle_timeout_seconds=300,
            max_age_seconds=1800,
            db_path=str(tmp_path / "elevation.db"),
        )
        self.rate_limiter = LoginRateLimiter()
        app = FastAPI()
        app.include_router(mcp_router)
        app.include_router(elevation_router)
        self.client = TestClient(app)
        self._secrets: Dict[str, str] = {}

    def add_user(self, username: str, role: UserRole, enroll_totp: bool) -> None:
        self.users.create_user(username, _PASSWORD, role)
        if enroll_totp:
            secret = self.totp.generate_secret(username)
            assert self.totp.activate_mfa(username, pyotp.TOTP(secret).now())
            self._secrets[username] = secret

    def login(self, username: str) -> str:
        user = self.users.get_user(username)
        assert user is not None
        return self.jwt.create_token(
            {"username": user.username, "role": user.role.value}
        )

    def valid_code(self, username: str) -> str:
        return str(pyotp.TOTP(self._secrets[username]).now())

    def wrong_code(self, username: str) -> str:
        totp = pyotp.TOTP(self._secrets[username])
        now = int(time.time())
        accepted = {totp.at(now + step * 30) for step in (-2, -1, 0, 1, 2)}
        for candidate in ("000000", "111111", "222222", "333333"):
            if candidate not in accepted:
                return candidate
        raise AssertionError("no rejected candidate code found")

    def call(self, token: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "error" not in body, f"JSON-RPC error for {name}: {body['error']}"
        content = body["result"]["content"]
        return json.loads(content[0]["text"])  # type: ignore[no-any-return]

    def rest_elevate(self, token: str, totp_code: str) -> Any:
        return self.client.post(
            "/auth/elevate",
            json={"totp_code": totp_code},
            headers={"Authorization": f"Bearer {token}"},
        )

    def tool_names(self, token: str) -> set:
        response = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        return {tool["name"] for tool in response.json()["result"]["tools"]}


def _jti(door: _FrontDoor, token: str) -> str:
    return str(door.jwt.validate_token(token)["jti"])


@pytest.fixture
def front_door(tmp_path: Path) -> Iterator[_FrontDoor]:
    # Construct the real app first so its startup wiring cannot overwrite
    # the auth globals patched below.
    real_state = real_app_module.app.state
    door = _FrontDoor(tmp_path)
    previous_totp: Optional[Any] = mfa_routes.get_totp_service()
    mfa_routes.set_totp_service(door.totp)
    try:
        with (
            patch.object(dependencies, "jwt_manager", door.jwt),
            patch.object(dependencies, "user_manager", door.users),
            patch.object(dependencies, "oauth_manager", None),
            patch.object(dependencies, "mcp_credential_manager", None),
            patch.object(dependencies, "server_config", None),
            patch.object(real_state, "group_manager", None, create=True),
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(f"{_DECORATOR}._is_elevation_enforcement_enabled", return_value=True),
            patch(f"{_HANDLER}._is_elevation_enforcement_enabled", return_value=True),
            patch(f"{_REST}._is_elevation_enforcement_enabled", return_value=True),
            patch(f"{_DECORATOR}.elevated_session_manager", door.esm),
            patch(f"{_HANDLER}.elevated_session_manager", door.esm),
            patch(f"{_REST}.elevated_session_manager", door.esm),
            patch(f"{_HANDLER}.login_rate_limiter", door.rate_limiter),
            patch(f"{_REST}.login_rate_limiter", door.rate_limiter),
        ):
            yield door
    finally:
        mfa_routes.set_totp_service(previous_totp)


class TestElevateSessionOverMcp:
    def test_gated_tool_proceeds_after_elevating_over_mcp(self, front_door):
        front_door.add_user("ops-admin", UserRole.ADMIN, enroll_totp=True)
        token = front_door.login("ops-admin")

        before = front_door.call(token, _GATED_TOOL, {"username": None})
        assert before.get("error") == "elevation_required"

        elevated = front_door.call(
            token, "elevate_session", {"totp_code": front_door.valid_code("ops-admin")}
        )
        assert elevated.get("elevated") is True
        assert elevated.get("scope") == "full"
        assert isinstance(elevated.get("elevated_until"), float)
        assert isinstance(elevated.get("max_until"), float)

        after = front_door.call(token, _GATED_TOOL, {"username": None})
        assert after.get("status") == "ok", after

    def test_wrong_code_opens_no_window(self, front_door):
        front_door.add_user("ops-admin", UserRole.ADMIN, enroll_totp=True)
        token = front_door.login("ops-admin")

        result = front_door.call(
            token, "elevate_session", {"totp_code": front_door.wrong_code("ops-admin")}
        )
        assert result.get("error") == "elevation_failed"

        gated = front_door.call(token, _GATED_TOOL, {"username": None})
        assert gated.get("error") == "elevation_required"

    def test_user_without_totp_gets_setup_required(self, front_door):
        front_door.add_user("no-mfa-admin", UserRole.ADMIN, enroll_totp=False)
        token = front_door.login("no-mfa-admin")

        result = front_door.call(token, "elevate_session", {"totp_code": "123456"})
        assert result.get("error") == "totp_setup_required"
        assert result.get("setup_url")

    def test_window_is_bound_to_the_token_that_opened_it(self, front_door):
        front_door.add_user("ops-admin", UserRole.ADMIN, enroll_totp=True)
        first_token = front_door.login("ops-admin")
        second_token = front_door.login("ops-admin")

        elevated = front_door.call(
            first_token,
            "elevate_session",
            {"totp_code": front_door.valid_code("ops-admin")},
        )
        assert elevated.get("elevated") is True

        other = front_door.call(second_token, _GATED_TOOL, {"username": None})
        assert other.get("error") == "elevation_required"

    def test_any_totp_enrolled_role_can_elevate(self, front_door):
        front_door.add_user("reader", UserRole.NORMAL_USER, enroll_totp=True)
        token = front_door.login("reader")

        assert "elevate_session" in front_door.tool_names(token)
        result = front_door.call(
            token, "elevate_session", {"totp_code": front_door.valid_code("reader")}
        )
        assert result.get("elevated") is True
        assert result.get("scope") == "full"

    def test_failed_attempts_share_one_lockout_across_mcp_and_rest(self, front_door):
        """Both front doors count failures under the same client-IP:username
        key, so failures split across them add up to one lockout."""
        front_door.add_user("ops-admin", UserRole.ADMIN, enroll_totp=True)
        token = front_door.login("ops-admin")
        wrong = front_door.wrong_code("ops-admin")

        for _ in range(3):
            result = front_door.call(token, "elevate_session", {"totp_code": wrong})
            assert result.get("error") == "elevation_failed"
        for _ in range(2):
            assert front_door.rest_elevate(token, wrong).status_code == 401

        valid = front_door.valid_code("ops-admin")
        mcp_result = front_door.call(token, "elevate_session", {"totp_code": valid})
        assert mcp_result.get("error") == "rate_limited"
        rest_response = front_door.rest_elevate(token, valid)
        assert rest_response.status_code == 429
        assert front_door.esm.get_status(_jti(front_door, token)) is None

    def test_non_admin_without_totp_gets_self_service_setup_url(self, front_door):
        front_door.add_user("reader", UserRole.NORMAL_USER, enroll_totp=False)
        token = front_door.login("reader")

        result = front_door.call(token, "elevate_session", {"totp_code": "123456"})
        assert result.get("error") == "totp_setup_required"
        assert result.get("setup_url") == dependencies._mfa_setup_url_for_role(
            UserRole.NORMAL_USER
        )
