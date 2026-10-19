"""``store_xray_pattern`` through the real ``POST /mcp`` JSON-RPC endpoint.

A real app (``create_app`` via ``isolated_app``) with real accounts; only the
cidx-meta location is pointed at a temporary git repository.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserManager, UserRole
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-Pattern-Passw0rd!"
READER = "example_pattern_reader"
WRITER = "example_pattern_writer"

pytestmark = pytest.mark.timeout(60)

_PATTERN = (
    "name: front-door-pattern\n"
    'description: "front door"\n'
    "language: java\n"
    "evaluator_code: |\n"
    "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    with isolated_app(tmp_path_factory.mktemp("store-pattern-app")) as app:
        accounts: UserManager = app.state.user_manager
        accounts.create_user(READER, PASSWORD, UserRole.NORMAL_USER)
        accounts.create_user(WRITER, PASSWORD, UserRole.POWER_USER)
        yield TestClient(app, follow_redirects=False)


@pytest.fixture
def cidx_meta(tmp_path: Path) -> Iterator[Path]:
    meta = tmp_path / "cidx-meta"
    meta.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=meta, check=True)
    with patch(
        "code_indexer.server.mcp.handlers.xray._store_pattern._get_cidx_meta_path",
        return_value=meta,
    ):
        yield meta


def _call(
    client: TestClient, username: str, arguments: Dict[str, Any]
) -> Dict[str, Any]:
    account = client.app.state.user_manager.get_user(username)  # type: ignore[attr-defined]
    assert account is not None and dependencies.jwt_manager is not None
    token = dependencies.jwt_manager.create_token(
        {"username": username, "role": account.role.value}
    )
    client.cookies.clear()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "store_xray_pattern", "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def test_normal_user_is_refused_at_the_front_door(
    client: TestClient, cidx_meta: Path
) -> None:
    body = _call(client, READER, {"scope": "__any__", "pattern_yaml": _PATTERN})
    assert "error" in body, body
    assert "repository:write" in body["error"]["message"]
    assert not (cidx_meta / "xray-patterns").exists()


def test_power_user_stores_through_the_front_door(
    client: TestClient, cidx_meta: Path
) -> None:
    body = _call(client, WRITER, {"scope": "__any__", "pattern_yaml": _PATTERN})
    assert "result" in body, body
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload.get("success") is True, payload
    assert (
        cidx_meta / "xray-patterns" / "__any__" / "front-door-pattern.yaml"
    ).exists()
    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=cidx_meta,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert WRITER in log
