"""GET /mcp SSE must accept gateway identity_forward JWTs.

Regression test for the fix that routes get_optional_user through
get_current_user_for_mcp (Priority 1.5 gateway JWT path) instead of
get_current_user (which lacks gateway JWT handling).
"""

import pytest
from starlette.requests import Request

from code_indexer.server.auth import dependencies as deps
from code_indexer.server.mcp.protocol import get_optional_user


class _FakeUser:
    username = "gwuser"


class _FakeAuthenticator:
    def __init__(self, *, handles, user=None, raise_error=None):
        self._handles = handles
        self._user = user
        self._raise = raise_error

    def handles(self, token):
        return self._handles

    async def authenticate(self, token):
        if self._raise is not None:
            raise self._raise
        return self._user


def _req(method: str, token: str | None = None) -> Request:
    headers = []
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/mcp",
            "query_string": b"",
            "headers": headers,
            "client": ("1.2.3.4", 1234),
            "state": {},
        }
    )


@pytest.fixture
def isolated_deps():
    names = (
        "gateway_identity_authenticator",
        "elevated_session_manager",
        "mcp_credential_manager",
        "user_manager",
        "server_config",
        "jwt_manager",
        "oauth_manager",
    )
    saved = {n: getattr(deps, n) for n in names}
    deps.mcp_credential_manager = None
    deps.elevated_session_manager = None
    deps.server_config = None
    deps.oauth_manager = None
    deps.user_manager = None
    deps.jwt_manager = None
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(deps, n, v)


async def test_get_optional_user_accepts_gateway_jwt(isolated_deps):
    """GET /mcp with a valid gateway identity_forward JWT returns the user."""
    user = _FakeUser()
    deps.gateway_identity_authenticator = _FakeAuthenticator(handles=True, user=user)
    result = await get_optional_user(_req("GET", "gw-token"))
    assert result is user


async def test_get_optional_user_returns_none_without_auth(isolated_deps):
    """GET /mcp without credentials returns None (not an exception)."""
    deps.gateway_identity_authenticator = None
    result = await get_optional_user(_req("GET"))
    assert result is None


async def test_get_optional_user_returns_none_on_invalid_token(isolated_deps):
    """GET /mcp with an invalid token returns None (swallowed 401)."""
    from code_indexer.server.auth.gateway_jwt_validator import GatewayTokenError

    deps.gateway_identity_authenticator = _FakeAuthenticator(
        handles=True, raise_error=GatewayTokenError("bad signature")
    )
    result = await get_optional_user(_req("GET", "bad-token"))
    assert result is None
