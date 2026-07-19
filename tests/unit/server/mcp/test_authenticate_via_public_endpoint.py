"""Regression test: 'authenticate' MCP tool via the UNAUTHENTICATED /mcp-public endpoint.

Second bug discovered while fixing the authenticated-/mcp-endpoint TypeError
(see test_authenticate_via_authenticated_endpoint.py): `handle_authenticate`
(src/code_indexer/server/mcp/handlers/admin/__init__.py) is a SYNC function
(`def`, not `async def`) that returns a plain dict via `_mcp_response(...)`.

`process_public_jsonrpc_request`'s `tool_name == "authenticate"` special case
(src/code_indexer/server/mcp/protocol.py) unconditionally does:
    result = await handler(params.get("arguments", {}), http_request, http_response)

Awaiting the return value of a sync function that returns a plain dict raises:
    TypeError: object dict can't be used in 'await' expression

This means the /mcp-public front door -- the ONLY way an unauthenticated MCP
client can log in -- was completely broken for the `authenticate` tool. This
test drives the REAL FastAPI app via TestClient against the real `/mcp-public`
route with a bogus API key and asserts a clean tool-level failure rather than
an `await`-related TypeError / JSON-RPC internal error.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """In-process FastAPI TestClient -- no external server required."""
    from code_indexer.server.app import app

    return TestClient(app)


def _call_authenticate_via_public_mcp_endpoint(client: TestClient):
    """Call the 'authenticate' tool through the UNAUTHENTICATED /mcp-public endpoint."""
    return client.post(
        "/mcp-public",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "authenticate",
                "arguments": {
                    "username": "admin",
                    "api_key": "cidx_sk_bogus_public_regression_test_key",
                },
            },
        },
    )


def _call_authenticate_via_public_mcp_endpoint_with_key(
    client: TestClient, api_key: str
):
    """Call the 'authenticate' tool via /mcp-public with a real, valid API key."""
    return client.post(
        "/mcp-public",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "authenticate",
                "arguments": {
                    "username": "admin",
                    "api_key": api_key,
                },
            },
        },
    )


@pytest.fixture
def admin_api_key(client):
    """Create a real, valid API key for the 'admin' user and clean it up after.

    Uses the SAME `dependencies.user_manager` singleton the real app (and
    `handle_authenticate`) reads from, so `validate_user_api_key` in the
    authenticate handler succeeds against a genuinely stored key -- not a
    mock. `client` is depended on only to guarantee
    `code_indexer.server.app` (which wires dependencies.user_manager at
    import time via create_app()) has already been imported.
    """
    from code_indexer.server.auth import dependencies as auth_deps
    from code_indexer.server.auth.api_key_manager import ApiKeyManager

    assert auth_deps.user_manager is not None, "user_manager not initialized"
    api_key_manager = ApiKeyManager(user_manager=auth_deps.user_manager)
    raw_key, key_id = api_key_manager.generate_key(
        "admin", name="test-authenticate-cookie-public-endpoint"
    )
    yield raw_key
    auth_deps.user_manager.delete_api_key("admin", key_id)


def _extract_cidx_session_set_cookie(resp) -> str:
    """Return the raw 'cidx_session=...' Set-Cookie header value, or ''."""
    if hasattr(resp.headers, "get_list"):
        candidates = resp.headers.get_list("set-cookie")
    else:
        single = resp.headers.get("set-cookie")
        candidates = [single] if single else []
    for header_value in candidates:
        if header_value.startswith("cidx_session="):
            return str(header_value)
    return ""


class TestAuthenticateViaPublicEndpoint:
    """Regression guard: authenticate tool must not crash via /mcp-public."""

    def test_authenticate_via_public_endpoint_does_not_crash(self, client):
        resp = _call_authenticate_via_public_mcp_endpoint(client)

        assert resp.status_code == 200, (
            f"Unexpected HTTP status: {resp.status_code} {resp.text[:400]}"
        )

        body = resp.json()

        # The bug manifests as a JSON-RPC -32603 internal error carrying a
        # TypeError about awaiting a non-awaitable dict. Assert it's absent.
        assert "error" not in body, (
            "authenticate crashed via /mcp-public -- "
            f"got JSON-RPC error instead of a tool result: {body.get('error')}"
        )
        assert "result" in body, f"Expected a JSON-RPC result, got: {body}"

        result = body["result"]

        # handle_authenticate's raw return value (via _mcp_response) is
        # returned directly here (no MCP content-wrapping, unlike the
        # authenticated /mcp path's handle_tools_call). Accept either shape
        # defensively, but require a clean success=False bogus-credentials
        # response either way.
        if isinstance(result, dict) and "content" in result:
            payload = json.loads(result["content"][0]["text"])
        else:
            payload = result

        assert payload.get("success") is False, (
            f"Expected success=False for a bogus API key, got: {payload}"
        )
        assert "error" in payload, f"Expected an error field, got: {payload}"

    def test_authenticate_via_public_endpoint_sets_session_cookie(
        self, client, admin_api_key
    ):
        """Valid admin credentials via /mcp-public must set the session cookie.

        /mcp-public is the ONLY front door an unauthenticated MCP client has
        to log in. This proves the security-relevant behavior the
        bogus-credentials test above cannot reach: `handle_authenticate` only
        calls `http_response.set_cookie(...)` on the success path.
        """
        resp = _call_authenticate_via_public_mcp_endpoint_with_key(
            client, admin_api_key
        )

        assert resp.status_code == 200, (
            f"Unexpected HTTP status: {resp.status_code} {resp.text[:400]}"
        )

        body = resp.json()
        assert "error" not in body, (
            f"authenticate crashed with valid credentials via /mcp-public: "
            f"{body.get('error')}"
        )
        assert "result" in body, f"Expected a JSON-RPC result, got: {body}"

        result = body["result"]
        if isinstance(result, dict) and "content" in result:
            payload = json.loads(result["content"][0]["text"])
        else:
            payload = result

        assert payload.get("success") is True, (
            f"Expected success=True for a valid API key, got: {payload}"
        )
        assert payload.get("username") == "admin", f"Unexpected payload: {payload}"

        set_cookie_header = _extract_cidx_session_set_cookie(resp)
        assert set_cookie_header, (
            "Expected a 'cidx_session' Set-Cookie header on successful "
            f"authenticate via /mcp-public, got Set-Cookie headers: "
            f"{resp.headers.get_list('set-cookie') if hasattr(resp.headers, 'get_list') else resp.headers.get('set-cookie')}"
        )
        assert "httponly" in set_cookie_header.lower(), (
            f"cidx_session cookie is not HttpOnly: {set_cookie_header}"
        )
