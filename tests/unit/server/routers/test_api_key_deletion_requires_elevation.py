"""
Deleting a personal API key requires the caller's own elevation window when
elevation enforcement is on, on REST (DELETE /api/keys/{key_id}) exactly as on
its MCP twin (delete_api_key). With enforcement off nothing changes.

Front doors: the real auth routes registered on a FastAPI app (TestClient)
and the real MCP JSON-RPC tools/call dispatcher, over real auth components
on isolated files (see self_service_elevation_harness).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.api_key_manager import ApiKeyManager
from code_indexer.server.routers.inline_auth import register_auth_routes
from code_indexer.server.web.auth import SESSION_COOKIE_NAME
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
    mcp_tools_call,
)

_USER = "apikey-delete-user"


@pytest.fixture
def stack(tmp_path, monkeypatch) -> SelfServiceStack:
    s = build_stack(tmp_path, monkeypatch)
    s.create_user(_USER)
    s.enroll_mfa(_USER)
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


def _new_key(stack: SelfServiceStack) -> str:
    _raw, key_id = ApiKeyManager(stack.user_manager).generate_key(_USER)
    return key_id


def _delete_via_session(client: TestClient, cookie: str, key_id: str):
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    try:
        return client.delete(f"/api/keys/{key_id}")
    finally:
        client.cookies.clear()


def _keys(stack: SelfServiceStack):
    return [k["key_id"] for k in stack.user_manager.get_api_keys(_USER)]


def test_rest_delete_without_window_is_refused(client, stack) -> None:
    key_id = _new_key(stack)
    user = stack.user_manager.get_user(_USER)
    with enforcement(True):
        resp = _delete_via_session(client, stack.session_cookie(user), key_id)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "elevation_required"
    assert _keys(stack) == [key_id]


def test_rest_delete_with_own_window_succeeds(client, stack) -> None:
    key_id = _new_key(stack)
    user = stack.user_manager.get_user(_USER)
    cookie = stack.session_cookie(user)
    stack.elevate(cookie, _USER)
    with enforcement(True):
        resp = _delete_via_session(client, cookie, key_id)
    assert resp.status_code == 200, resp.text
    assert _keys(stack) == []


def test_rest_delete_unchanged_with_enforcement_off(client, stack) -> None:
    key_id = _new_key(stack)
    user = stack.user_manager.get_user(_USER)
    with enforcement(False):
        resp = _delete_via_session(client, stack.session_cookie(user), key_id)
    assert resp.status_code == 200, resp.text


def test_mcp_twin_is_refused_the_same_way(stack) -> None:
    key_id = _new_key(stack)
    user = stack.user_manager.get_user(_USER)
    with enforcement(True):
        payload = asyncio.run(
            mcp_tools_call("delete_api_key", {"key_id": key_id}, user, None)
        )
    assert payload["error"] == "elevation_required"
    assert _keys(stack) == [key_id]
