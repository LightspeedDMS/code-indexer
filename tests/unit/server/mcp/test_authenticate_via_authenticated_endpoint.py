"""Regression test: 'authenticate' MCP tool via the AUTHENTICATED /mcp endpoint.

Root cause (pre-existing bug, exposed as a loud server log by Bug #1423 / v11.61.0):

`handle_authenticate` (src/code_indexer/server/mcp/handlers/admin/__init__.py)
has a special 3-positional-argument signature:
    handle_authenticate(args, http_request, http_response) -> Dict[str, Any]
It needs `http_response` to set an HttpOnly `cidx_session` cookie.

The UNAUTHENTICATED `/mcp-public` endpoint (`process_public_jsonrpc_request`)
special-cases `tool_name == "authenticate"` and calls the handler correctly with
real `http_request`/`http_response` objects.

The AUTHENTICATED `/mcp` endpoint's generic dispatch chain
(`mcp_endpoint` -> `process_jsonrpc_request` -> `handle_tools_call` ->
`_invoke_handler`) has NO such special case. Because `authenticate`'s tool doc
declares `required_permission: public`, ANY authenticated user can reach it via
`/mcp`, and the generic `_invoke_handler` ends up calling
`handle_authenticate(arguments, user)` -- binding `user` into the
`http_request` slot and leaving `http_response` unfilled, raising:
    TypeError: handle_authenticate() missing 1 required positional argument:
    'http_response'

This is caught by `process_jsonrpc_request`'s generic `except Exception`
handler and surfaces as a JSON-RPC -32603 internal error (and, since Bug
#1423, a loud server-side ERROR log).

This test drives the REAL FastAPI app via TestClient against the real `/mcp`
route (not `/mcp-public`) with a valid authenticated admin session, calling
the `authenticate` tool with a bogus API key. It asserts the response is a
clean tool-level failure (`success: False`, e.g. "Invalid credentials")
rather than a JSON-RPC internal error / TypeError.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """In-process FastAPI TestClient -- no external server required."""
    from code_indexer.server.app import app

    return TestClient(app)


@pytest.fixture(scope="module")
def admin_token(client):
    """Authenticate as admin and return JWT access token."""
    resp = client.post(
        "/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    assert resp.status_code == 200, (
        f"Login failed: {resp.status_code} {resp.text[:200]}"
    )
    return resp.json()["access_token"]


@pytest.fixture
def admin_api_key(client, admin_token):
    """Create a real, valid API key for the 'admin' user and clean it up after.

    Uses the SAME `dependencies.user_manager` singleton the real app (and
    `handle_authenticate`) reads from, so `validate_user_api_key` in the
    authenticate handler succeeds against a genuinely stored key -- not a
    mock. `admin_token` is depended on only to guarantee the app has already
    been fully constructed (module import of code_indexer.server.app wires
    dependencies.user_manager at import time).
    """
    from code_indexer.server.auth import dependencies as auth_deps
    from code_indexer.server.auth.api_key_manager import ApiKeyManager

    assert auth_deps.user_manager is not None, "user_manager not initialized"
    api_key_manager = ApiKeyManager(user_manager=auth_deps.user_manager)
    raw_key, key_id = api_key_manager.generate_key(
        "admin", name="test-authenticate-cookie-authenticated-endpoint"
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
            return header_value
    return ""


def _call_authenticate_via_authenticated_mcp_endpoint(client: TestClient, token: str):
    """Call the 'authenticate' tool through the AUTHENTICATED /mcp endpoint.

    Deliberately hits `/mcp` (not `/mcp-public`) with a Bearer token so the
    request flows through the generic `handle_tools_call`/`_invoke_handler`
    dispatch chain that lacks the authenticate special-case.
    """
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "authenticate",
                "arguments": {
                    "username": "admin",
                    "api_key": "cidx_sk_bogus_regression_test_key",
                },
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _call_authenticate_via_authenticated_mcp_endpoint_with_key(
    client: TestClient, token: str, api_key: str
):
    """Call the 'authenticate' tool via /mcp with a real, valid API key."""
    return client.post(
        "/mcp",
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
        headers={"Authorization": f"Bearer {token}"},
    )


class TestAuthenticateViaAuthenticatedEndpoint:
    """Regression guard: authenticate tool must not crash via /mcp."""

    def test_authenticate_via_authenticated_endpoint_does_not_crash(
        self, client, admin_token
    ):
        resp = _call_authenticate_via_authenticated_mcp_endpoint(client, admin_token)

        # Transport-level: JSON-RPC over HTTP always returns 200 for a
        # well-formed request, even for tool-level or JSON-RPC-level errors.
        assert resp.status_code == 200, (
            f"Unexpected HTTP status: {resp.status_code} {resp.text[:400]}"
        )

        body = resp.json()

        # The bug manifests as a JSON-RPC -32603 internal error carrying a
        # TypeError about the missing 'http_response' argument. Assert that
        # NEITHER symptom is present.
        assert "error" not in body, (
            "authenticate crashed via the authenticated /mcp endpoint -- "
            f"got JSON-RPC error instead of a tool result: {body['error']}"
        )
        assert "result" in body, f"Expected a JSON-RPC result, got: {body}"

        result = body["result"]
        assert isinstance(result, dict) and "content" in result, (
            f"Expected MCP content-wrapped result, got: {result}"
        )
        text = result["content"][0]["text"]
        assert "missing 1 required positional argument" not in text, (
            f"authenticate handler crashed with TypeError: {text}"
        )
        assert "http_response" not in text, (
            f"authenticate handler leaked TypeError about http_response: {text}"
        )

        # Positive assertion: the handler ran its real bogus-credentials
        # branch and returned a clean tool-level failure.
        payload = json.loads(text)
        assert payload.get("success") is False, (
            f"Expected success=False for a bogus API key, got: {payload}"
        )
        assert "error" in payload, f"Expected an error field, got: {payload}"

    def test_authenticate_via_authenticated_endpoint_sets_session_cookie(
        self, client, admin_token, admin_api_key
    ):
        """Valid admin credentials via /mcp must set the HttpOnly session cookie.

        This is the security-relevant behavior the bogus-credentials test above
        cannot reach: `handle_authenticate` only calls
        `http_response.set_cookie(...)` on the success path. Without this test,
        a regression that broke cookie-setting while still returning
        success=True (or vice versa) would go undetected.
        """
        resp = _call_authenticate_via_authenticated_mcp_endpoint_with_key(
            client, admin_token, admin_api_key
        )

        assert resp.status_code == 200, (
            f"Unexpected HTTP status: {resp.status_code} {resp.text[:400]}"
        )

        body = resp.json()
        assert "error" not in body, (
            f"authenticate crashed with valid credentials: {body.get('error')}"
        )
        assert "result" in body, f"Expected a JSON-RPC result, got: {body}"

        result = body["result"]
        assert isinstance(result, dict) and "content" in result, (
            f"Expected MCP content-wrapped result, got: {result}"
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload.get("success") is True, (
            f"Expected success=True for a valid API key, got: {payload}"
        )
        assert payload.get("username") == "admin", f"Unexpected payload: {payload}"

        set_cookie_header = _extract_cidx_session_set_cookie(resp)
        assert set_cookie_header, (
            "Expected a 'cidx_session' Set-Cookie header on successful "
            f"authenticate, got Set-Cookie headers: "
            f"{resp.headers.get_list('set-cookie') if hasattr(resp.headers, 'get_list') else resp.headers.get('set-cookie')}"
        )
        assert "httponly" in set_cookie_header.lower(), (
            f"cidx_session cookie is not HttpOnly: {set_cookie_header}"
        )
