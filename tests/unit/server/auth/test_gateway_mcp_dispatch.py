"""EVO-66121: the /mcp auth dispatch routes trusted-gateway bearer tokens.

Verifies get_current_user_for_mcp's Priority 1.5 branch: a token the gateway
authenticator claims is validated+provisioned into a User; an invalid gateway token
becomes 401; a non-gateway token is left alone (authenticate never called) and falls
through to the existing OAuth/JWT path.
"""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from code_indexer.server.auth import dependencies as deps
from code_indexer.server.auth.gateway_jwt_validator import GatewayTokenError


def _req(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("1.2.3.4", 1234),
            "state": {},
        }
    )


class _FakeUser:
    username = "gwuser"


class _FakeAuthenticator:
    def __init__(self, *, handles, user=None, raise_error=None):
        self._handles = handles
        self._user = user
        self._raise = raise_error
        self.authenticate_called = False

    def handles(self, token):
        return self._handles

    async def authenticate(self, token):
        self.authenticate_called = True
        if self._raise is not None:
            raise self._raise
        return self._user


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
    # Neutralise every other auth path so the gateway branch is the only live one.
    deps.mcp_credential_manager = None  # Priority 1 (MCP creds) -> None
    deps.elevated_session_manager = None  # skip elevation window creation
    deps.server_config = None  # skip non-SSO restriction check
    deps.oauth_manager = None
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(deps, n, v)


async def test_gateway_token_returns_provisioned_user(isolated_deps):
    user = _FakeUser()
    deps.gateway_identity_authenticator = _FakeAuthenticator(handles=True, user=user)
    result = await deps.get_current_user_for_mcp(_req("gw-token"))
    assert result is user


async def test_invalid_gateway_token_is_401(isolated_deps):
    deps.gateway_identity_authenticator = _FakeAuthenticator(
        handles=True, raise_error=GatewayTokenError("bad signature")
    )
    with pytest.raises(HTTPException) as exc:
        await deps.get_current_user_for_mcp(_req("gw-token"))
    assert exc.value.status_code == 401


async def test_non_gateway_token_falls_through_untouched(isolated_deps):
    # handles() False -> authenticate must never run; request falls through to the
    # existing OAuth/JWT path, which (unconfigured here) rejects with an HTTPException.
    deps.user_manager = None
    deps.jwt_manager = None
    fake = _FakeAuthenticator(handles=False)
    deps.gateway_identity_authenticator = fake
    with pytest.raises(HTTPException):
        await deps.get_current_user_for_mcp(_req("some-other-token"))
    assert fake.authenticate_called is False


async def test_no_authenticator_wired_is_noop(isolated_deps):
    # Default deployment (gateway auth disabled) -> None -> branch skipped entirely.
    deps.gateway_identity_authenticator = None
    deps.user_manager = None
    deps.jwt_manager = None
    with pytest.raises(HTTPException):
        await deps.get_current_user_for_mcp(_req("anything"))
