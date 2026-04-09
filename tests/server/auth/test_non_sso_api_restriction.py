"""Tests for Story #563: Non-SSO Admin REST/MCP Restriction.

When restrict_non_sso_to_web_ui is enabled, non-SSO accounts are denied
access to REST API and MCP endpoints (HTTP 403). SSO accounts are unaffected.
Toggle is OFF by default.
"""

import asyncio
import json
import os
import sys
import tempfile
import types

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials

import code_indexer.server.auth.dependencies as deps_module
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.utils.config_manager import (
    ServerConfig,
    WebSecurityConfig,
)


@pytest.fixture(autouse=True)
def fake_app_module(monkeypatch):
    """Provide a lightweight fake app module with token blacklist functions."""
    fake_app = types.ModuleType("code_indexer.server.app")
    token_blacklist: set = set()

    def blacklist_token(jti: str) -> None:
        token_blacklist.add(jti)

    def is_token_blacklisted(jti: str) -> bool:
        return jti in token_blacklist

    fake_app.blacklist_token = blacklist_token  # type: ignore[attr-defined]
    fake_app.is_token_blacklisted = is_token_blacklisted  # type: ignore[attr-defined]
    fake_app.token_blacklist = token_blacklist  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "code_indexer.server.app", fake_app)
    try:
        yield
    finally:
        sys.modules.pop("code_indexer.server.app", None)


@pytest.fixture
def temp_users_file():
    """Create temporary users file."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w") as f:
        json.dump({}, f)
    try:
        yield path
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.fixture
def user_manager(temp_users_file):
    """Create user manager with temp file."""
    return UserManager(users_file_path=temp_users_file)


@pytest.fixture
def setup_auth_env(user_manager):
    """Set real managers on dependencies module."""
    jwt_mgr = JWTManager(secret_key="test-non-sso-restriction-key")

    deps_module.jwt_manager = jwt_mgr
    deps_module.user_manager = user_manager
    deps_module.oauth_manager = None
    deps_module.mcp_credential_manager = None

    sys.modules["code_indexer.server.app"].token_blacklist.clear()

    yield jwt_mgr

    deps_module.jwt_manager = None
    deps_module.user_manager = None
    deps_module.oauth_manager = None
    deps_module.mcp_credential_manager = None
    deps_module.server_config = None
    sys.modules["code_indexer.server.app"].token_blacklist.clear()


def _make_bearer_request(token: str) -> Request:
    """Construct a request with Bearer token in Authorization header."""
    headers = [
        (b"authorization", f"Bearer {token}".encode("latin-1")),
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }
    return Request(scope)


def _make_cookie_request(token: str) -> Request:
    """Construct a request with JWT cookie only (no session cookie)."""
    headers = [
        (b"cookie", f"cidx_session={token}".encode("latin-1")),
    ]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/repos",
        "headers": headers,
    }
    return Request(scope)


def _make_config(restrict: bool, server_dir: str = "/tmp/cidx") -> ServerConfig:
    """Create a ServerConfig with the restriction toggle set."""
    return ServerConfig(
        server_dir=server_dir,
        web_security_config=WebSecurityConfig(
            restrict_non_sso_to_web_ui=restrict,
        ),
    )


def _create_sso_user(user_manager: UserManager, username: str, role: UserRole) -> None:
    """Create a user and mark them as SSO (OIDC identity)."""
    password = "StrongP@ssw0rd-SSO"
    user_manager.create_user(username, password, role)
    user_manager.set_oidc_identity(
        username,
        {
            "subject": f"sub-{username}",
            "email": f"{username}@example.com",
            "linked_at": "2025-01-01T00:00:00",
            "last_login": "2025-01-01T00:00:00",
        },
    )


class TestNonSsoRestrictionDisabled:
    """When restrict_non_sso_to_web_ui is False (default), all users pass."""

    def test_non_sso_user_allowed_via_bearer_when_disabled(
        self, setup_auth_env, user_manager
    ):
        """Non-SSO user can access REST/MCP via Bearer when toggle is OFF."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("local_user", "StrongP@ssw0rd-1", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "local_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=False)

        request = _make_bearer_request(token)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        user = deps_module.get_current_user(request=request, credentials=credentials)
        assert user is not None
        assert user.username == "local_user"

    def test_non_sso_user_allowed_via_cookie_when_disabled(
        self, setup_auth_env, user_manager
    ):
        """Non-SSO user can access via JWT cookie when toggle is OFF."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("local_user", "StrongP@ssw0rd-1", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "local_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=False)

        request = _make_cookie_request(token)
        user = deps_module.get_current_user(request=request, credentials=None)
        assert user is not None
        assert user.username == "local_user"

    def test_default_config_has_restriction_disabled(self):
        """WebSecurityConfig defaults to restrict_non_sso_to_web_ui=False."""
        config = WebSecurityConfig()
        assert config.restrict_non_sso_to_web_ui is False


class TestNonSsoRestrictionEnabled:
    """When restrict_non_sso_to_web_ui is True, non-SSO users get 403."""

    def test_non_sso_user_denied_via_bearer_when_enabled(
        self, setup_auth_env, user_manager
    ):
        """Non-SSO user gets 403 from REST/MCP via Bearer when toggle ON."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("local_user", "StrongP@ssw0rd-1", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "local_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=True)

        request = _make_bearer_request(token)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        with pytest.raises(Exception) as exc_info:
            deps_module.get_current_user(request=request, credentials=credentials)
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]
        assert "restricted to Web UI" in exc_info.value.detail  # type: ignore[attr-defined]

    def test_non_sso_user_denied_via_cookie_when_enabled(
        self, setup_auth_env, user_manager
    ):
        """Non-SSO user gets 403 via JWT cookie when toggle ON."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("local_user", "StrongP@ssw0rd-1", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "local_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=True)

        request = _make_cookie_request(token)

        with pytest.raises(Exception) as exc_info:
            deps_module.get_current_user(request=request, credentials=None)
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]
        assert "restricted to Web UI" in exc_info.value.detail  # type: ignore[attr-defined]

    def test_sso_user_allowed_via_bearer_when_enabled(
        self, setup_auth_env, user_manager
    ):
        """SSO user can access REST/MCP via Bearer when toggle is ON."""
        jwt_mgr = setup_auth_env
        _create_sso_user(user_manager, "sso_user", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "sso_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=True)

        request = _make_bearer_request(token)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        user = deps_module.get_current_user(request=request, credentials=credentials)
        assert user is not None
        assert user.username == "sso_user"

    def test_non_sso_admin_also_denied_when_enabled(self, setup_auth_env, user_manager):
        """Non-SSO admin gets 403 -- restriction applies to all non-SSO."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("admin_local", "StrongP@ssw0rd-3", UserRole.ADMIN)
        token = jwt_mgr.create_token({"username": "admin_local", "role": "admin"})

        deps_module.server_config = _make_config(restrict=True)

        request = _make_bearer_request(token)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        with pytest.raises(Exception) as exc_info:
            deps_module.get_current_user(request=request, credentials=credentials)
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]


class TestNonSsoRestrictionMcpEndpoint:
    """Tests for get_current_user_for_mcp() with non-SSO restriction."""

    def test_non_sso_user_denied_via_mcp_when_enabled(
        self, setup_auth_env, user_manager
    ):
        """Non-SSO user gets 403 from MCP endpoint when toggle is ON."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("local_user", "StrongP@ssw0rd-1", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "local_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=True)

        request = _make_bearer_request(token)

        with pytest.raises(Exception) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                deps_module.get_current_user_for_mcp(request=request)
            )
        assert exc_info.value.status_code == 403  # type: ignore[attr-defined]
        assert "restricted to Web UI" in exc_info.value.detail  # type: ignore[attr-defined]

    def test_sso_user_allowed_via_mcp_when_enabled(self, setup_auth_env, user_manager):
        """SSO user can access MCP endpoint even when toggle is ON."""
        jwt_mgr = setup_auth_env
        _create_sso_user(user_manager, "sso_user", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "sso_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=True)

        request = _make_bearer_request(token)

        user = asyncio.get_event_loop().run_until_complete(
            deps_module.get_current_user_for_mcp(request=request)
        )
        assert user is not None
        assert user.username == "sso_user"

    def test_non_sso_user_allowed_via_mcp_when_disabled(
        self, setup_auth_env, user_manager
    ):
        """Non-SSO user can access MCP when toggle is OFF."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("local_user", "StrongP@ssw0rd-1", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "local_user", "role": "normal_user"})

        deps_module.server_config = _make_config(restrict=False)

        request = _make_bearer_request(token)

        user = asyncio.get_event_loop().run_until_complete(
            deps_module.get_current_user_for_mcp(request=request)
        )
        assert user is not None
        assert user.username == "local_user"


class TestNonSsoRestrictionNoConfig:
    """When server_config is None, restriction does not apply."""

    def test_non_sso_user_allowed_when_no_config(self, setup_auth_env, user_manager):
        """Non-SSO user passes through when server_config is None."""
        jwt_mgr = setup_auth_env
        user_manager.create_user("local_user", "StrongP@ssw0rd-1", UserRole.NORMAL_USER)
        token = jwt_mgr.create_token({"username": "local_user", "role": "normal_user"})

        deps_module.server_config = None

        request = _make_bearer_request(token)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        user = deps_module.get_current_user(request=request, credentials=credentials)
        assert user is not None
        assert user.username == "local_user"
