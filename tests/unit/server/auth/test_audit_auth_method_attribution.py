"""Each authentication branch records how the caller authenticated.

The request's audit holder (placed by ``AuditRequestContextMiddleware``) gets
``auth_method`` from the dependency branch that actually authenticated the
caller, so every audit row written during the request carries it.  The
routes below run the REAL dependencies against real JWT, user, OAuth and MCP
credential managers.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Dict, Iterator, Optional

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import code_indexer.server.auth.dependencies as deps
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager
from code_indexer.server.auth.oauth.oauth_manager import OAuthManager
from code_indexer.server.auth.user_manager import User, UserManager, UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
    current_audit_request_context,
)

_USERNAME = "example-user"
_PASSWORD = "SecureP@ssw0rd!XyZ789"


def _holder_auth_method() -> Dict[str, Optional[str]]:
    ctx = current_audit_request_context()
    assert ctx is not None
    return {"auth_method": ctx.auth_method}


@pytest.fixture()
def managers(tmp_path: Path, monkeypatch) -> Iterator[Dict[str, object]]:
    jwt_manager = JWTManager(secret_key="example-secret-for-audit-auth-method")
    user_manager = UserManager(users_file_path=str(tmp_path / "users.json"))
    user_manager.create_user(
        username=_USERNAME, password=_PASSWORD, role=UserRole.NORMAL_USER
    )
    oauth_manager = OAuthManager(
        db_path=str(tmp_path / "oauth.db"),
        issuer="http://localhost:8000",
        user_manager=user_manager,
    )
    mcp_manager = MCPCredentialManager(user_manager=user_manager)
    monkeypatch.setattr(deps, "jwt_manager", jwt_manager)
    monkeypatch.setattr(deps, "user_manager", user_manager)
    monkeypatch.setattr(deps, "oauth_manager", oauth_manager)
    monkeypatch.setattr(deps, "mcp_credential_manager", mcp_manager)
    monkeypatch.setattr(deps, "elevated_session_manager", None)
    yield {
        "jwt": jwt_manager,
        "users": user_manager,
        "oauth": oauth_manager,
        "mcp": mcp_manager,
    }


@pytest.fixture()
def client(managers) -> TestClient:
    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)

    @app.get("/api/who")
    def api_who(user: User = Depends(deps.get_current_user)):
        return _holder_auth_method()

    @app.get("/api/who-web-or-api")
    def web_or_api_who(user: User = Depends(deps.get_current_user_web_or_api)):
        return _holder_auth_method()

    @app.post("/mcp/who")
    async def mcp_who(user: User = Depends(deps.get_current_user_for_mcp)):
        return _holder_auth_method()

    return TestClient(app)


def _jwt(managers) -> str:
    return str(
        managers["jwt"].create_token(
            user_data={"username": _USERNAME, "role": "normal_user"}
        )
    )


def _oauth_access_token(managers) -> str:
    oauth = managers["oauth"]
    info = oauth.register_client(
        client_name="Example Client", redirect_uris=["http://localhost/callback"]
    )
    verifier = "example-verifier-" + "x" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    code = oauth.generate_authorization_code(
        client_id=info["client_id"],
        user_id=_USERNAME,
        code_challenge=challenge,
        redirect_uri=info["redirect_uris"][0],
        state="example-state",
    )
    tokens = oauth.exchange_code_for_token(
        code=code, code_verifier=verifier, client_id=info["client_id"]
    )
    return str(tokens["access_token"])


def test_bearer_jwt_records_jwt(client, managers) -> None:
    body = client.get(
        "/api/who", headers={"Authorization": f"Bearer {_jwt(managers)}"}
    ).json()
    assert body == {"auth_method": "jwt"}


def test_oauth_access_token_records_oauth_token(client, managers) -> None:
    token = _oauth_access_token(managers)
    body = client.get("/api/who", headers={"Authorization": f"Bearer {token}"}).json()
    assert body == {"auth_method": "oauth_token"}


def test_session_jwt_cookie_records_web_session(client, managers) -> None:
    client.cookies.set(deps.CIDX_SESSION_COOKIE, _jwt(managers))
    body = client.get("/api/who").json()
    assert body == {"auth_method": "web_session"}


def test_mcp_credential_records_mcp_credential(client, managers) -> None:
    cred = managers["mcp"].generate_credential(_USERNAME, name="example")
    basic = base64.b64encode(
        f"{cred['client_id']}:{cred['client_secret']}".encode()
    ).decode()
    body = client.post("/mcp/who", headers={"Authorization": f"Basic {basic}"}).json()
    assert body == {"auth_method": "mcp_credential"}


def test_mcp_bearer_jwt_records_jwt_across_the_thread_offload(client, managers) -> None:
    body = client.post(
        "/mcp/who", headers={"Authorization": f"Bearer {_jwt(managers)}"}
    ).json()
    assert body == {"auth_method": "jwt"}


def test_web_or_api_bearer_records_jwt(client, managers) -> None:
    body = client.get(
        "/api/who-web-or-api", headers={"Authorization": f"Bearer {_jwt(managers)}"}
    ).json()
    assert body == {"auth_method": "jwt"}
