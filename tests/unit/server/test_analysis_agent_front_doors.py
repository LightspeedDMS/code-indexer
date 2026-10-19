"""The analysis agents' account is usable only through its MCP credential on /mcp.

Real app (``create_app`` via ``isolated_app``), real accounts and real MCP
credentials. The agent account is given a KNOWN password here only to prove
that even the correct password is refused; a normal account with the same
password and its own MCP credential is the control for every door.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.analysis_agent_principal import (
    ANALYSIS_AGENT_TOOLS,
    ANALYSIS_AGENT_USERNAME,
)
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-Agent-Door-Passw0rd!"
CONTROL = "example_door_control"

pytestmark = pytest.mark.timeout(60)


@pytest.fixture(scope="module")
def world(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Tuple[TestClient, Dict[str, Dict[str, str]]]]:
    with isolated_app(tmp_path_factory.mktemp("agent-doors-app")) as app:
        accounts: UserManager = app.state.user_manager
        creds: Dict[str, Dict[str, str]] = {}
        for name in (ANALYSIS_AGENT_USERNAME, CONTROL):
            accounts.create_user(name, PASSWORD, UserRole.NORMAL_USER)
            assert dependencies.mcp_credential_manager is not None
            creds[name] = dependencies.mcp_credential_manager.generate_credential(
                name, "example"
            )
        yield TestClient(app, follow_redirects=False), creds


def _basic(cred: Dict[str, str]) -> Dict[str, str]:
    raw = f"{cred['client_id']}:{cred['client_secret']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def _bearer(username: str) -> Dict[str, str]:
    assert dependencies.jwt_manager is not None
    token = dependencies.jwt_manager.create_token(
        {"username": username, "role": UserRole.NORMAL_USER.value}
    )
    return {"Authorization": f"Bearer {token}"}


def _rpc(client: TestClient, headers: Dict[str, str], method: str) -> Any:
    client.cookies.clear()
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        headers=headers,
    )


def test_password_login_is_refused_even_with_the_right_password(world) -> None:
    client, _ = world
    client.cookies.clear()
    agent = client.post(
        "/auth/login", json={"username": ANALYSIS_AGENT_USERNAME, "password": PASSWORD}
    )
    control = client.post(
        "/auth/login", json={"username": CONTROL, "password": PASSWORD}
    )
    assert control.status_code == 200, control.text
    assert agent.status_code == 401, agent.text


def test_client_credentials_grant_is_refused(world) -> None:
    client, creds = world

    def _grant(name: str) -> Any:
        return client.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": creds[name]["client_id"],
                "client_secret": creds[name]["client_secret"],
            },
        )

    assert _grant(CONTROL).status_code == 200
    refused = _grant(ANALYSIS_AGENT_USERNAME)
    assert refused.status_code in (400, 401), refused.text
    assert "access_token" not in refused.text


def test_a_bearer_token_is_refused_on_rest_and_mcp(world) -> None:
    client, _ = world
    client.cookies.clear()
    control = client.get("/api/repos", headers=_bearer(CONTROL))
    assert control.status_code not in (401, 403), control.text
    assert client.get(
        "/api/repos", headers=_bearer(ANALYSIS_AGENT_USERNAME)
    ).status_code in (401, 403)
    mcp = _rpc(client, _bearer(ANALYSIS_AGENT_USERNAME), "tools/list")
    assert mcp.status_code in (401, 403), mcp.text


def test_the_mcp_credential_works_and_lists_only_the_allowlist(world) -> None:
    client, creds = world
    response = _rpc(client, _basic(creds[ANALYSIS_AGENT_USERNAME]), "tools/list")
    assert response.status_code == 200, response.text
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names and names <= ANALYSIS_AGENT_TOOLS, names - ANALYSIS_AGENT_TOOLS
    control = _rpc(client, _basic(creds[CONTROL]), "tools/list")
    control_names = {t["name"] for t in control.json()["result"]["tools"]}
    assert control_names - ANALYSIS_AGENT_TOOLS, "control must see more tools"
    assert json.dumps(sorted(names))


def _web_session_request(username: str) -> Any:
    from fastapi import Response
    from starlette.requests import Request

    from code_indexer.server.web.auth import get_session_manager

    response = Response()
    get_session_manager().create_session(response, username, "normal_user")
    cookie = response.headers["set-cookie"].split(";", 1)[0]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/example",
        "headers": [(b"cookie", cookie.encode())],
        "client": ("192.0.2.10", 5000),
    }
    return Request(scope)


def test_a_web_session_cookie_is_refused(world) -> None:
    from fastapi import HTTPException

    control_user = dependencies._hybrid_auth_impl(_web_session_request(CONTROL), None)
    assert control_user.username == CONTROL
    assert (
        dependencies.get_current_user_web_or_api(
            _web_session_request(CONTROL), None
        ).username
        == CONTROL
    )
    for door in (
        dependencies._hybrid_auth_impl,
        dependencies.get_current_user_web_or_api,
    ):
        with pytest.raises(HTTPException) as refused:
            door(_web_session_request(ANALYSIS_AGENT_USERNAME), None)
        assert refused.value.status_code in (401, 403)
