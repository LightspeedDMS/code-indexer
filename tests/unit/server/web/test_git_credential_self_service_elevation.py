"""
Managing your own git-forge credential (configure, update, delete) requires
TOTP plus the caller's own elevation window when elevation enforcement is on,
on the web REST routes (/user/git-credentials) and on MCP
(configure_git_credential / delete_git_credential) alike. Listing is
unchanged on both. With enforcement off nothing changes.

Front doors: the real user web router (TestClient) and the real MCP
JSON-RPC tools/call dispatcher, over real auth components on isolated files
and the real GitCredentialManager. Only the external forge API client is
replaced (it would otherwise call the forge over the network).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.mcp.handlers.git_write import _get_credential_manager
from code_indexer.server.web.auth import SESSION_COOKIE_NAME
from code_indexer.server.web.routes import user_router
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
    mcp_tools_call,
)

_FORGE_CLIENT_PATH = (
    "code_indexer.server.services.git_credential_manager.get_forge_client"
)
_FORGE_HOST = "forge.example.com"
_MCP_KEY = "mcp-elevation-key-git-credentials"


class _FakeForgeClient:
    """Stands in for the external forge API (identity discovery)."""

    def __init__(self) -> None:
        self.calls = 0

    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
        self.calls += 1
        return {
            "git_user_name": "Example User",
            "git_user_email": "user@example.com",
            "forge_username": "example-user",
        }


@pytest.fixture
def stack(tmp_path, monkeypatch) -> SelfServiceStack:
    return build_stack(tmp_path, monkeypatch)


@pytest.fixture
def forge():
    fake = _FakeForgeClient()
    with patch(_FORGE_CLIENT_PATH, return_value=fake):
        yield fake


@pytest.fixture
def client(stack) -> TestClient:
    app = FastAPI()
    app.include_router(user_router, prefix="/user")
    return TestClient(app, raise_server_exceptions=False)


def _new_user(stack: SelfServiceStack, with_mfa: bool):
    user = stack.create_user(f"gitcred-{uuid.uuid4().hex[:10]}")
    if with_mfa:
        stack.enroll_mfa(user.username)
    return user


def _stored(username: str) -> List[Dict[str, Any]]:
    creds: List[Dict[str, Any]] = _get_credential_manager().list_credentials(username)
    return creds


def _seed(username: str) -> str:
    """Store a credential directly (setup only) and return its id."""
    manager = _get_credential_manager()
    credential_id = str(uuid.uuid4())
    manager._backend.upsert_credential(
        credential_id=credential_id,
        username=username,
        forge_type="github",
        forge_host=_FORGE_HOST,
        encrypted_token=manager._encrypt_token("seeded-token-0000"),
        git_user_name=None,
        git_user_email=None,
        forge_username=None,
        name="seeded",
    )
    return credential_id


def _rest(client: TestClient, cookie: str, method: str, path: str, **kwargs):
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    try:
        return client.request(method, path, **kwargs)
    finally:
        client.cookies.clear()


_ADD_BODY = {"forge_type": "github", "forge_host": _FORGE_HOST, "token": "tok-1234"}


# ---------------------------------------------------------------------------
# REST /user/git-credentials
# ---------------------------------------------------------------------------


class TestRestGitCredentialsEnforcementOn:
    def test_add_without_window_is_refused(self, client, stack, forge):
        user = _new_user(stack, with_mfa=True)
        with enforcement(True):
            resp = _rest(
                client,
                stack.session_cookie(user),
                "POST",
                "/user/git-credentials",
                json=_ADD_BODY,
            )

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "elevation_required"
        assert forge.calls == 0
        assert _stored(user.username) == []

    def test_add_with_own_window_succeeds(self, client, stack, forge):
        user = _new_user(stack, with_mfa=True)
        cookie = stack.session_cookie(user)
        stack.elevate(cookie, user.username)
        with enforcement(True):
            resp = _rest(
                client, cookie, "POST", "/user/git-credentials", json=_ADD_BODY
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["success"] is True
        assert len(_stored(user.username)) == 1

    def test_add_without_mfa_is_sent_to_totp_setup(self, client, stack, forge):
        user = _new_user(stack, with_mfa=False)
        with enforcement(True):
            resp = _rest(
                client,
                stack.session_cookie(user),
                "POST",
                "/user/git-credentials",
                json=_ADD_BODY,
            )

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == {
            "error": "totp_setup_required",
            "setup_url": "/user/mfa/setup",
        }
        assert _stored(user.username) == []

    def test_delete_without_window_is_refused(self, client, stack):
        user = _new_user(stack, with_mfa=True)
        credential_id = _seed(user.username)
        with enforcement(True):
            resp = _rest(
                client,
                stack.session_cookie(user),
                "DELETE",
                f"/user/git-credentials/{credential_id}",
            )

        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "elevation_required"
        assert len(_stored(user.username)) == 1

    def test_delete_with_own_window_succeeds(self, client, stack):
        user = _new_user(stack, with_mfa=True)
        credential_id = _seed(user.username)
        cookie = stack.session_cookie(user)
        stack.elevate(cookie, user.username)
        with enforcement(True):
            resp = _rest(
                client, cookie, "DELETE", f"/user/git-credentials/{credential_id}"
            )

        assert resp.status_code == 200, resp.text
        assert _stored(user.username) == []

    def test_listing_needs_no_window(self, client, stack):
        user = _new_user(stack, with_mfa=True)
        _seed(user.username)
        with enforcement(True):
            resp = _rest(
                client,
                stack.session_cookie(user),
                "GET",
                "/user/partials/git-credentials-list",
            )

        assert resp.status_code == 200, resp.text
        assert _FORGE_HOST in resp.text


class TestRestGitCredentialsEnforcementOff:
    def test_add_and_delete_without_window_are_unchanged(self, client, stack, forge):
        user = _new_user(stack, with_mfa=False)
        cookie = stack.session_cookie(user)
        with enforcement(False):
            added = _rest(
                client, cookie, "POST", "/user/git-credentials", json=_ADD_BODY
            )
            assert added.status_code == 200, added.text
            credential_id = added.json()["credential_id"]
            deleted = _rest(
                client, cookie, "DELETE", f"/user/git-credentials/{credential_id}"
            )

        assert deleted.status_code == 200, deleted.text
        assert _stored(user.username) == []


class TestRestGitCredentialsUnauthenticated:
    """The routes answer an unauthenticated request themselves, with the same
    body as before, whether enforcement is on or off."""

    @pytest.mark.parametrize("enforced", [True, False])
    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            ("POST", "/user/git-credentials", {"json": _ADD_BODY}),
            ("DELETE", "/user/git-credentials/some-credential-id", {}),
        ],
    )
    def test_reports_session_expired(
        self, client, stack, forge, enforced, method, path, kwargs
    ):
        with enforcement(enforced):
            resp = client.request(method, path, **kwargs)

        assert resp.status_code == 401, resp.text
        assert resp.json() == {"success": False, "error": "Session expired"}
        assert forge.calls == 0


# ---------------------------------------------------------------------------
# MCP configure_git_credential / delete_git_credential / list_git_credentials
# ---------------------------------------------------------------------------

_MCP_ARGS = {"forge_type": "github", "forge_host": _FORGE_HOST, "token": "tok-5678"}


class TestMcpGitCredentials:
    @pytest.mark.asyncio
    async def test_configure_without_window_is_refused(self, stack, forge):
        user = _new_user(stack, with_mfa=True)
        with enforcement(True):
            data = await mcp_tools_call(
                "configure_git_credential", dict(_MCP_ARGS), user, _MCP_KEY
            )

        assert data["error"] == "elevation_required"
        assert forge.calls == 0
        assert _stored(user.username) == []

    @pytest.mark.asyncio
    async def test_configure_with_own_window_succeeds(self, stack, forge):
        user = _new_user(stack, with_mfa=True)
        stack.elevate(_MCP_KEY, user.username)
        with enforcement(True):
            data = await mcp_tools_call(
                "configure_git_credential", dict(_MCP_ARGS), user, _MCP_KEY
            )

        assert data["success"] is True, data
        assert len(_stored(user.username)) == 1

    @pytest.mark.asyncio
    async def test_configure_without_mfa_requires_totp_setup(self, stack, forge):
        user = _new_user(stack, with_mfa=False)
        with enforcement(True):
            data = await mcp_tools_call(
                "configure_git_credential", dict(_MCP_ARGS), user, _MCP_KEY
            )

        assert data["error"] == "totp_setup_required"
        assert _stored(user.username) == []

    @pytest.mark.asyncio
    async def test_delete_without_window_is_refused(self, stack):
        user = _new_user(stack, with_mfa=True)
        credential_id = _seed(user.username)
        with enforcement(True):
            data = await mcp_tools_call(
                "delete_git_credential",
                {"credential_id": credential_id},
                user,
                _MCP_KEY,
            )

        assert data["error"] == "elevation_required"
        assert len(_stored(user.username)) == 1

    @pytest.mark.asyncio
    async def test_delete_with_own_window_succeeds(self, stack):
        user = _new_user(stack, with_mfa=True)
        credential_id = _seed(user.username)
        stack.elevate(_MCP_KEY, user.username)
        with enforcement(True):
            data = await mcp_tools_call(
                "delete_git_credential",
                {"credential_id": credential_id},
                user,
                _MCP_KEY,
            )

        assert data["success"] is True, data
        assert _stored(user.username) == []

    @pytest.mark.asyncio
    async def test_list_needs_no_window(self, stack):
        user = _new_user(stack, with_mfa=True)
        _seed(user.username)
        with enforcement(True):
            data = await mcp_tools_call("list_git_credentials", {}, user, _MCP_KEY)

        assert data["success"] is True, data
        assert data["count"] == 1

    @pytest.mark.asyncio
    async def test_enforcement_off_is_unchanged(self, stack, forge):
        user = _new_user(stack, with_mfa=False)
        with enforcement(False):
            added = await mcp_tools_call(
                "configure_git_credential", dict(_MCP_ARGS), user, None
            )
            assert added["success"] is True, added
            deleted = await mcp_tools_call(
                "delete_git_credential",
                {"credential_id": added["credential_id"]},
                user,
                None,
            )

        assert deleted["success"] is True, deleted
        assert _stored(user.username) == []
