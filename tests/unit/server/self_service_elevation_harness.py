"""Shared harness for self-service elevation front-door tests.

Wires REAL auth components onto isolated files under a pytest tmp_path:
UserManager (JSON store), TOTPService, ElevatedSessionManager, the web
SessionManager and a JWTManager, and points the process-wide singletons the
REST dependencies, the web routes and the MCP elevation decorator read at
them (via monkeypatch, so every singleton is restored after the test).

Only the elevation-enforcement switch is patched, at both of its read
points (REST dependencies and the MCP decorator).
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Iterator, Optional, Tuple
from unittest.mock import patch

import pyotp
from fastapi import Response

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.api_key_manager import ApiKeyManager
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import User, UserManager, UserRole
from code_indexer.server.mcp.auth import elevation_decorator
from code_indexer.server.web import auth as web_auth
from code_indexer.server.web import mfa_routes
from code_indexer.server.web.auth import SESSION_COOKIE_NAME, SessionManager

_REST_ENFORCEMENT = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_MCP_ENFORCEMENT = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TEST_PASSWORD = "Harness-Pass-1234!"


@dataclass
class SelfServiceStack:
    user_manager: UserManager
    totp: TOTPService
    esm: ElevatedSessionManager
    session_manager: SessionManager
    jwt_manager: JWTManager

    def create_user(self, username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
        return self.user_manager.create_user(username, _TEST_PASSWORD, role)

    def enroll_mfa(self, username: str) -> str:
        secret = self.totp.generate_secret(username)
        assert self.totp.activate_mfa(username, pyotp.TOTP(secret).now())
        return secret

    def session_cookie(self, user: User) -> str:
        resp = Response()
        self.session_manager.create_session(
            resp, username=user.username, role=user.role.value
        )
        header = resp.headers["set-cookie"]
        prefix = f"{SESSION_COOKIE_NAME}="
        assert header.startswith(prefix), header
        return header[len(prefix) :].split(";", 1)[0]

    def bearer(self, user: User) -> Tuple[str, str]:
        """Return (jwt, jti) for `user`."""
        token = self.jwt_manager.create_token(
            {"username": user.username, "role": user.role.value}
        )
        jti = self.jwt_manager.validate_token(token)["jti"]
        return token, str(jti)

    def elevate(self, session_key: str, username: str, scope: str = "full") -> None:
        self.esm.create(
            session_key=session_key,
            username=username,
            elevated_from_ip=None,
            scope=scope,
        )


def build_stack(tmp_path: Any, monkeypatch: Any) -> SelfServiceStack:
    from code_indexer.server.mcp.handlers import _utils as mcp_utils

    # Resolve the app module's lazily-built services FIRST: building them
    # rewires the dependencies singletons, which must happen before the
    # isolated components below are installed over them.
    getattr(mcp_utils.app_module, "user_manager")

    user_manager = UserManager(users_file_path=str(tmp_path / "users.json"))
    totp = TOTPService(db_path=str(tmp_path / "mfa.db"))
    esm = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevated.db"),
    )
    session_manager = SessionManager(
        "harness-signing-key", SimpleNamespace(host="127.0.0.1")
    )
    jwt_manager = JWTManager(secret_key="harness-jwt-secret")

    monkeypatch.setattr(dependencies, "user_manager", user_manager)
    monkeypatch.setattr(dependencies, "jwt_manager", jwt_manager)
    monkeypatch.setattr(dependencies, "oauth_manager", None)
    monkeypatch.setattr(dependencies, "server_config", None)
    monkeypatch.setattr(
        dependencies, "api_key_manager", ApiKeyManager(user_manager=user_manager)
    )
    monkeypatch.setattr(dependencies, "elevated_session_manager", esm)
    monkeypatch.setattr(elevation_decorator, "elevated_session_manager", esm)
    monkeypatch.setattr(mfa_routes, "elevated_session_manager", esm)
    monkeypatch.setattr(mfa_routes, "_totp_service", totp)
    monkeypatch.setattr(web_auth, "_session_manager", session_manager)
    monkeypatch.setattr(mcp_utils.app_module, "user_manager", user_manager)
    return SelfServiceStack(user_manager, totp, esm, session_manager, jwt_manager)


@contextlib.contextmanager
def enforcement(enabled: bool) -> Iterator[None]:
    with (
        patch(_REST_ENFORCEMENT, return_value=enabled),
        patch(_MCP_ENFORCEMENT, return_value=enabled),
    ):
        yield


async def mcp_tools_call(
    tool: str,
    arguments: Dict[str, Any],
    user: User,
    elevation_key: Optional[str],
) -> Dict[str, Any]:
    """Drive `tool` through the real MCP JSON-RPC tools/call dispatcher and
    return the tool's decoded JSON payload."""
    from code_indexer.server.mcp.protocol import process_jsonrpc_request

    response = await process_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        user,
        elevation_key=elevation_key,
    )
    assert "result" in response, response
    content = response["result"]["content"]
    payload: Dict[str, Any] = json.loads(content[0]["text"])
    return payload
