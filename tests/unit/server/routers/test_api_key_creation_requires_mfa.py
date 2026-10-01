"""
Creating a personal API key requires MFA plus the caller's own elevation
window when elevation enforcement is on -- on REST (POST /api/keys) and MCP
(create_api_key) alike. With enforcement off nothing changes. Listing keys
and using existing keys are unchanged either way.

Front doors: the real auth routes registered on a FastAPI app (TestClient)
and the real MCP JSON-RPC tools/call dispatcher, over real auth components
on isolated files (see self_service_elevation_harness).
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.routers.inline_auth import register_auth_routes
from code_indexer.server.web.auth import SESSION_COOKIE_NAME
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
    mcp_tools_call,
)

_WITH_MFA = "apikey-with-mfa"
_WITHOUT_MFA = "apikey-without-mfa"


@pytest.fixture
def stack(tmp_path, monkeypatch) -> SelfServiceStack:
    s = build_stack(tmp_path, monkeypatch)
    s.create_user(_WITH_MFA)
    s.enroll_mfa(_WITH_MFA)
    s.create_user(_WITHOUT_MFA)
    return s


@pytest.fixture
def client(stack) -> TestClient:
    app = FastAPI()
    register_auth_routes(
        app,
        jwt_manager=stack.jwt_manager,
        user_manager=stack.user_manager,
        refresh_token_manager=None,
    )
    return TestClient(app, raise_server_exceptions=False)


def _create_via_session(client: TestClient, cookie: str):
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    try:
        return client.post("/api/keys", json={"name": "ci-key"})
    finally:
        client.cookies.clear()


def _key_count(stack: SelfServiceStack, username: str) -> int:
    return len(stack.user_manager.get_api_keys(username))


def _detail(resp) -> Dict[str, Any]:
    detail: Dict[str, Any] = resp.json()["detail"]
    return detail


# ---------------------------------------------------------------------------
# REST POST /api/keys
# ---------------------------------------------------------------------------


class TestRestCreateApiKeyEnforcementOn:
    def test_with_mfa_without_window_is_refused(self, client, stack):
        user = stack.user_manager.get_user(_WITH_MFA)
        with enforcement(True):
            resp = _create_via_session(client, stack.session_cookie(user))

        assert resp.status_code == 403, resp.text
        assert _detail(resp)["error"] == "elevation_required"
        assert _key_count(stack, _WITH_MFA) == 0

    def test_with_mfa_with_own_session_window_succeeds(self, client, stack):
        user = stack.user_manager.get_user(_WITH_MFA)
        cookie = stack.session_cookie(user)
        stack.elevate(cookie, _WITH_MFA)
        with enforcement(True):
            resp = _create_via_session(client, cookie)

        assert resp.status_code == 201, resp.text
        assert resp.json()["api_key"].startswith("cidx_sk_")
        assert _key_count(stack, _WITH_MFA) == 1

    def test_window_owned_by_another_user_is_refused(self, client, stack):
        user = stack.user_manager.get_user(_WITH_MFA)
        cookie = stack.session_cookie(user)
        stack.elevate(cookie, "someone-else")
        with enforcement(True):
            resp = _create_via_session(client, cookie)

        assert resp.status_code == 403, resp.text
        assert _detail(resp)["error"] == "elevation_required"
        assert _key_count(stack, _WITH_MFA) == 0

    def test_bearer_jwt_with_window_keyed_by_its_jti_succeeds(self, client, stack):
        """/auth/elevate keys a Bearer caller's window by the token's jti; the
        gate must find that same window."""
        user = stack.user_manager.get_user(_WITH_MFA)
        token, jti = stack.bearer(user)
        stack.elevate(jti, _WITH_MFA)
        with enforcement(True):
            resp = client.post(
                "/api/keys",
                json={"name": "bearer-key"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 201, resp.text
        assert _key_count(stack, _WITH_MFA) == 1

    def test_bearer_jwt_without_window_is_refused(self, client, stack):
        user = stack.user_manager.get_user(_WITH_MFA)
        token, _jti = stack.bearer(user)
        with enforcement(True):
            resp = client.post(
                "/api/keys",
                json={"name": "bearer-key"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 403, resp.text
        assert _detail(resp)["error"] == "elevation_required"
        assert _key_count(stack, _WITH_MFA) == 0

    def test_without_mfa_is_sent_to_totp_setup(self, client, stack):
        user = stack.user_manager.get_user(_WITHOUT_MFA)
        with enforcement(True):
            resp = _create_via_session(client, stack.session_cookie(user))

        assert resp.status_code == 403, resp.text
        assert _detail(resp) == {
            "error": "totp_setup_required",
            "setup_url": "/user/mfa/setup",
        }
        assert _key_count(stack, _WITHOUT_MFA) == 0

    def test_listing_keys_needs_no_window(self, client, stack):
        user = stack.user_manager.get_user(_WITHOUT_MFA)
        client.cookies.set(SESSION_COOKIE_NAME, stack.session_cookie(user))
        with enforcement(True):
            resp = client.get("/api/keys")
        client.cookies.clear()

        assert resp.status_code == 200, resp.text
        assert resp.json()["keys"] == []

    def test_existing_key_keeps_authenticating(self, client, stack):
        """A key minted before enforcement still authenticates afterwards."""
        user = stack.user_manager.get_user(_WITHOUT_MFA)
        with enforcement(False):
            created = _create_via_session(client, stack.session_cookie(user))
        assert created.status_code == 201, created.text
        raw_key = created.json()["api_key"]

        with enforcement(True):
            resp = client.get(
                "/api/keys", headers={"Authorization": f"Bearer {raw_key}"}
            )

        assert resp.status_code == 200, resp.text
        assert len(resp.json()["keys"]) == 1


class TestRestCreateApiKeyEnforcementOff:
    @pytest.mark.parametrize("username", [_WITH_MFA, _WITHOUT_MFA])
    def test_creation_without_window_is_unchanged(self, client, stack, username):
        user = stack.user_manager.get_user(username)
        with enforcement(False):
            resp = _create_via_session(client, stack.session_cookie(user))

        assert resp.status_code == 201, resp.text
        assert _key_count(stack, username) == 1


# ---------------------------------------------------------------------------
# MCP create_api_key (tools/call) -- same rules as REST
# ---------------------------------------------------------------------------

_MCP_KEY = "mcp-elevation-key-apikeys"


class TestMcpCreateApiKey:
    @pytest.mark.asyncio
    async def test_enforcement_on_without_window_is_refused(self, stack):
        user = stack.user_manager.get_user(_WITH_MFA)
        with enforcement(True):
            data = await mcp_tools_call(
                "create_api_key", {"description": "mcp-key"}, user, _MCP_KEY
            )

        assert data["error"] == "elevation_required"
        assert _key_count(stack, _WITH_MFA) == 0

    @pytest.mark.asyncio
    async def test_enforcement_on_with_own_window_succeeds(self, stack):
        user = stack.user_manager.get_user(_WITH_MFA)
        stack.elevate(_MCP_KEY, _WITH_MFA)
        with enforcement(True):
            data = await mcp_tools_call(
                "create_api_key", {"description": "mcp-key"}, user, _MCP_KEY
            )

        assert data["success"] is True, data
        assert _key_count(stack, _WITH_MFA) == 1

    @pytest.mark.asyncio
    async def test_enforcement_on_without_mfa_requires_totp_setup(self, stack):
        user = stack.user_manager.get_user(_WITHOUT_MFA)
        with enforcement(True):
            data = await mcp_tools_call(
                "create_api_key", {"description": "mcp-key"}, user, _MCP_KEY
            )

        assert data["error"] == "totp_setup_required"
        assert _key_count(stack, _WITHOUT_MFA) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("username", [_WITH_MFA, _WITHOUT_MFA])
    async def test_enforcement_off_is_unchanged(self, stack, username):
        user = stack.user_manager.get_user(username)
        with enforcement(False):
            data = await mcp_tools_call(
                "create_api_key", {"description": "mcp-key"}, user, None
            )

        assert data["success"] is True, data
        assert _key_count(stack, username) == 1


def test_admin_role_is_not_required(client, stack):
    """Self-service gate: an admin is gated on its own TOTP/window exactly
    like any other user (no extra role requirement)."""
    admin = stack.create_user("apikey-admin", UserRole.ADMIN)
    stack.enroll_mfa(admin.username)
    cookie = stack.session_cookie(admin)
    stack.elevate(cookie, admin.username)
    with enforcement(True):
        resp = _create_via_session(client, cookie)

    assert resp.status_code == 201, resp.text
