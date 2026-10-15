"""A stored-secret write refused during a mixed-version cluster upgrade
(no readable legacy key file while the cluster gate is closed) is answered
at every git-credential front door with a clear retry message: Web 409,
MCP success false; at most one WARNING and no traceback.

Front doors: the real user and admin web routers (TestClient) and the real
MCP tools/call dispatcher over real auth components on isolated files. The
cluster gate is closed through a test double (the state an older node in
the cluster produces); the external forge API client is replaced.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.mcp.handlers.git_write import _get_credential_manager
from code_indexer.server.services.stored_secret_key import StoredSecretCipher
from code_indexer.server.web.auth import SESSION_COOKIE_NAME
from code_indexer.server.web.routes import user_router, web_router
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
_BODY = {"forge_type": "github", "forge_host": _FORGE_HOST, "token": "tok-1234"}
_RETRY_TEXT = "upgrade in progress"


class _FakeForgeClient:
    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
        return {
            "git_user_name": "Example User",
            "git_user_email": "user@example.com",
            "forge_username": "example-user",
        }


@pytest.fixture
def stack(tmp_path: Any, monkeypatch: Any) -> SelfServiceStack:
    return build_stack(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def forge() -> Iterator[None]:
    with patch(_FORGE_CLIENT_PATH, return_value=_FakeForgeClient()):
        yield


@pytest.fixture
def gate_closed() -> Iterator[None]:
    """A cluster node older than 12.90 is still running."""
    with patch.object(StoredSecretCipher, "v2_writes_allowed", return_value=False):
        yield


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(user_router, prefix="/user")
    app.include_router(web_router, prefix="/admin")
    return TestClient(app, raise_server_exceptions=False)


_DOOR_LOGGERS = (
    "code_indexer.server.web.routes",
    "code_indexer.server.mcp.handlers.git_write",
)


def _assert_quiet(caplog: pytest.LogCaptureFixture) -> None:
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name in _DOOR_LOGGERS
    ]
    assert len(warnings) <= 1, [r.getMessage() for r in warnings]
    assert all(r.exc_info is None for r in warnings)
    assert "tok-1234" not in caplog.text


@pytest.mark.parametrize(
    "path,role",
    [
        ("/user/git-credentials", UserRole.NORMAL_USER),
        ("/admin/git-credentials", UserRole.ADMIN),
    ],
)
def test_web_door_answers_409_with_the_retry_message(
    stack: SelfServiceStack,
    gate_closed: None,
    caplog: pytest.LogCaptureFixture,
    path: str,
    role: UserRole,
) -> None:
    user = stack.create_user(f"refused-{uuid.uuid4().hex[:10]}", role)
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, stack.session_cookie(user))
    caplog.set_level(logging.WARNING)
    with enforcement(False):
        resp = client.post(path, json=_BODY)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["success"] is False
    assert _RETRY_TEXT in body["error"].lower()
    assert _get_credential_manager().list_credentials(user.username) == []
    _assert_quiet(caplog)


@pytest.mark.asyncio
async def test_mcp_door_answers_failure_with_the_retry_message(
    stack: SelfServiceStack, gate_closed: None, caplog: pytest.LogCaptureFixture
) -> None:
    user = stack.create_user(f"refused-{uuid.uuid4().hex[:10]}")
    caplog.set_level(logging.WARNING)
    with enforcement(False):
        data = await mcp_tools_call(
            "configure_git_credential",
            dict(_BODY),
            user,
            "mcp-key-legacy-write-refused",
        )
    assert data["success"] is False
    assert _RETRY_TEXT in data["error"].lower()
    assert _get_credential_manager().list_credentials(user.username) == []
    _assert_quiet(caplog)
