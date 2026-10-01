# ruff: noqa: F811
"""
Smoke tests: Health, Auth, OAuth, MCP protocol, Favicon (Story #409).

Covers route groups:
- Health & status routes
- Auth endpoints (/auth/*, /api/auth/*)
- OAuth endpoints (/oauth/*, /.well-known/*)
- MCP protocol (/mcp, /mcp-public)
- Favicon & login web pages
"""

import pytest

from tests.unit.server.routes._smoke_registry import (
    _smoke,
    client,  # noqa: F401 — fixture re-export
    mock_admin_user,  # noqa: F401 — fixture re-export
)


# ---------------------------------------------------------------------------
# Group 1: Health & Status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/api/system/health",
        "/cache/stats",
    ],
)
def test_get_health_routes(client, path):
    _smoke(client, "GET", path)


# ---------------------------------------------------------------------------
# Group 2: Auth endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,body",
    [
        ("/auth/login", {"username": "x", "password": "y"}),
        ("/auth/register", {"username": "x", "password": "y", "role": "normal_user"}),
        ("/auth/refresh", {"refresh_token": "bad-token"}),
        ("/auth/reset-password", {"username": "x", "new_password": "y"}),
        ("/api/auth/refresh", {"refresh_token": "bad-token"}),
    ],
)
def test_post_auth_routes(client, path, body):
    _smoke(client, "POST", path, json=body)


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/login/sso",
        "/admin/login",
        "/admin/logout",
        "/user/login",
        "/user/logout",
    ],
)
def test_get_login_web_routes(client, path):
    _smoke(client, "GET", path)


def test_post_web_login(client):
    _smoke(client, "POST", "/login", data={"username": "x", "password": "y"})


# ---------------------------------------------------------------------------
# Group 3: OAuth endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/oauth/.well-known/oauth-authorization-server",
        "/oauth/authorize",
    ],
)
def test_get_oauth_routes(client, path):
    _smoke(client, "GET", path)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/oauth/authorize", {}),
        ("/oauth/authorize/consent", {}),
        ("/oauth/register", {"client_name": "test", "redirect_uris": []}),
        ("/oauth/revoke", {"token": "bad"}),
        ("/oauth/token", {"grant_type": "authorization_code", "code": "x"}),
    ],
)
def test_post_oauth_routes(client, path, body):
    _smoke(client, "POST", path, json=body)


# ---------------------------------------------------------------------------
# Group 4: MCP protocol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        # GET /mcp-public deliberately excluded (see EXCLUDED_ROUTES in
        # _smoke_registry.py, Bug #1628): it is a genuine SSE stream whose
        # response body never completes by design, so the naive blocking
        # `_smoke()` helper hangs forever on it. A streaming-aware dedicated
        # test was evaluated but is not viable here: Starlette's TestClient
        # (starlette/testclient.py) runs the whole ASGI call synchronously
        # to completion before returning any response to httpx, regardless
        # of whether `.get()` or `.stream()` is used — so any call against
        # this infinite-generator endpoint hangs identically either way.
    ],
)
def test_get_mcp_routes(client, path):
    _smoke(client, "GET", path)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        (
            "/mcp-public",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        ),
    ],
)
def test_post_mcp_routes(client, path, body):
    _smoke(client, "POST", path, json=body)


# ---------------------------------------------------------------------------
# Group 5: Favicon & cache handle
# ---------------------------------------------------------------------------


def test_get_favicon(client):
    _smoke(client, "GET", "/favicon.ico")


def test_get_cache_handle(client):
    _smoke(client, "GET", "/cache/test-handle")
