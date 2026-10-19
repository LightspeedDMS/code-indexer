"""Only the admins group may activate the internal catalog repository, or
use an activated copy of it.

Driven through the real ``POST /mcp`` endpoint of a real app (``create_app``
via ``isolated_app``, never ~/.cidx-server). The services the tools reach
are the real ones (QueryAccessEnv): AccessFilteringService over a real
GroupAccessManager, real ActivatedRepoManager activations (real git clones),
GoldenRepoManager, global registry and alias files. Only the external
embedding + HNSW search boundary is replaced.

A refusal must answer exactly what the door answers for a repository the
caller may not use (activation) or an alias the caller never activated
(an existing copy), so it never reveals more than those refusals do.

Repository aliases and usernames are neutral placeholders.
"""

from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.mcp.session_registry import get_session_registry
from code_indexer.server.services.constants import CIDX_META_REPO
from code_indexer.server.services.file_service import file_service
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    ALL_ROWS_LIMIT,
    CATALOG_FILE,
    CATALOG_TEXT,
    GRANTED_REPO,
    UNGRANTED_REPO,
    USER,
    QueryAccessEnv,
    build_server_db_template,
    global_alias,
    register_catalog,
)

PASSWORD = "Example-Activation-Passw0rd!"
POWER = "example_power"
META_GLOBAL = global_alias(CIDX_META_REPO)
META_COPY = "my-catalog"
ADMIN_META_COPY = "admin-catalog"
OWN_ACTIVATION = "my-repo"
UNKNOWN_ALIAS = "never-activated-repo"
README = "README.md"
README_TEXT = "example"

pytestmark = pytest.mark.timeout(45)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    """The real app over an isolated server home (never ~/.cidx-server)."""
    with isolated_app(tmp_path_factory.mktemp("meta-activation-app")) as app:
        accounts: UserManager = app.state.user_manager
        accounts.create_user(USER, PASSWORD, UserRole.NORMAL_USER)
        accounts.create_user(POWER, PASSWORD, UserRole.POWER_USER)
        accounts.create_user(ADMIN, PASSWORD, UserRole.ADMIN)
        yield TestClient(app, follow_redirects=False)


@pytest.fixture(scope="module")
def server_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_server_db_template(tmp_path_factory.mktemp("server_db_template"))


@pytest.fixture
def env(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_db_template: Path,
) -> Iterator[QueryAccessEnv]:
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    e = QueryAccessEnv(tmp_path, server_db_template)
    e.group_manager.assign_user_to_group(POWER, e.group_id, assigned_by="test")
    register_catalog(e)
    monkeypatch.setattr(
        file_service, "_activated_repo_manager_lazy", e.activated_repo_manager
    )
    try:
        with e.installed(e.access_service):
            state = vars(importlib.import_module("code_indexer.server.app"))[
                "app"
            ].state
            state.user_manager = client.app.state.user_manager  # type: ignore[attr-defined]
            state.activated_repo_manager = e.activated_repo_manager
            yield e
    finally:
        e.close()


def _mcp(
    client: TestClient,
    username: str,
    tool: str,
    arguments: Dict[str, Any],
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One ``tools/call`` through ``POST /mcp``; the JSON-RPC body."""
    account = client.app.state.user_manager.get_user(username)  # type: ignore[attr-defined]
    assert account is not None
    jwt = dependencies.jwt_manager
    assert jwt is not None
    token = jwt.create_token({"username": username, "role": account.role.value})
    headers = {"Authorization": f"Bearer {token}"}
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    client.cookies.clear()
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def _payload(body: Dict[str, Any]) -> Dict[str, Any]:
    assert "result" in body, body
    payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return payload


def _refusal(body: Dict[str, Any], alias: str) -> Dict[str, Any]:
    """The JSON-RPC refusal, with the requested alias masked."""
    assert "error" in body, body
    error: Dict[str, Any] = body["error"]
    assert "Access denied" in error["message"], error
    return {**error, "message": error["message"].replace(f"'{alias}'", "'<alias>'")}


def _impersonating(client: TestClient, target: str) -> str:
    """An ADMIN MCP session that impersonates *target*; its session id."""
    accounts = client.app.state.user_manager  # type: ignore[attr-defined]
    session_id = f"example-session-{uuid.uuid4()}"
    session = get_session_registry().get_or_create_session(
        session_id, accounts.get_user(ADMIN)
    )
    session.set_impersonation(accounts.get_user(target))
    return session_id


def _activations(env: QueryAccessEnv, username: str) -> List[str]:
    return [
        str(a["user_alias"])
        for a in env.activated_repo_manager.list_activated_repositories(username)
    ]


def _activate(golden: str, user_alias: str) -> Dict[str, Any]:
    return {"golden_repo_alias": golden, "user_alias": user_alias}


def _file_text(payload: Dict[str, Any]) -> str:
    assert payload["success"] is True, payload
    return "".join(str(block.get("text", "")) for block in payload["file_content"])


class TestActivationRefused:
    @pytest.mark.parametrize("golden", [CIDX_META_REPO, META_GLOBAL])
    def test_normal_user_activation_is_refused_like_an_ungranted_repo(
        self, client, env, golden
    ):
        meta = _mcp(client, USER, "activate_repository", _activate(golden, "copy"))
        ungranted = _mcp(
            client, USER, "activate_repository", _activate(UNGRANTED_REPO, "copy")
        )

        assert _refusal(meta, golden) == _refusal(ungranted, UNGRANTED_REPO)
        assert _activations(env, USER) == []

    def test_composite_containing_the_catalog_is_refused(self, client, env):
        body = _mcp(
            client,
            POWER,
            "activate_repository",
            {
                "golden_repo_aliases": [GRANTED_REPO, CIDX_META_REPO],
                "user_alias": "my-composite",
            },
        )

        _refusal(body, CIDX_META_REPO)
        assert _activations(env, POWER) == []

    def test_manage_composite_create_containing_the_catalog_is_refused(
        self, client, env
    ):
        body = _mcp(
            client,
            POWER,
            "manage_composite_repository",
            {
                "operation": "create",
                "user_alias": "my-composite",
                "golden_repo_aliases": [GRANTED_REPO, CIDX_META_REPO],
            },
        )

        _refusal(body, CIDX_META_REPO)
        assert _activations(env, POWER) == []

    def test_json_array_string_aliases_are_refused_like_an_ungranted_repo(
        self, client, env
    ):
        """Some MCP clients send lists as JSON-encoded strings."""

        def _args(golden: str) -> Dict[str, Any]:
            return {
                "golden_repo_aliases": json.dumps([GRANTED_REPO, golden]),
                "user_alias": "my-composite",
            }

        meta = _mcp(client, POWER, "activate_repository", _args(CIDX_META_REPO))
        ungranted = _mcp(client, POWER, "activate_repository", _args(UNGRANTED_REPO))

        assert _refusal(meta, CIDX_META_REPO) == _refusal(ungranted, UNGRANTED_REPO)
        assert _activations(env, POWER) == []

    def test_admin_impersonating_a_normal_user_is_refused(self, client, env):
        session_id = _impersonating(client, USER)

        body = _mcp(
            client,
            ADMIN,
            "activate_repository",
            _activate(CIDX_META_REPO, "copy"),
            session_id,
        )

        _refusal(body, CIDX_META_REPO)
        assert _activations(env, USER) == []


class TestActivationAllowed:
    def test_admin_activation_of_the_catalog_succeeds(self, client, env):
        payload = _payload(
            _mcp(
                client,
                ADMIN,
                "activate_repository",
                _activate(CIDX_META_REPO, ADMIN_META_COPY),
            )
        )

        assert payload["success"] is True, payload
        status = env.wait_for_job(payload["job_id"], ADMIN)
        assert status["status"] == "completed", status
        assert _activations(env, ADMIN) == [ADMIN_META_COPY]

    def test_normal_user_activation_of_a_granted_repo_succeeds(self, client, env):
        payload = _payload(
            _mcp(
                client,
                USER,
                "activate_repository",
                _activate(GRANTED_REPO, OWN_ACTIVATION),
            )
        )

        assert payload["success"] is True, payload
        status = env.wait_for_job(payload["job_id"], USER)
        assert status["status"] == "completed", status


CONTENT_DOORS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "get_file_content": lambda a: {"repository_alias": a, "file_path": CATALOG_FILE},
    "search_code": lambda a: {
        "query_text": "find",
        "repository_alias": a,
        "limit": ALL_ROWS_LIMIT,
    },
    "regex_search": lambda a: {"pattern": CATALOG_TEXT, "repository_alias": a},
    "git_log": lambda a: {"repository_alias": a},
    "git_search_diffs": lambda a: {"repository_alias": a, "search_string": "catalog"},
    "git_file_at_revision": lambda a: {
        "repository_alias": a,
        "path": CATALOG_FILE,
        "revision": "HEAD",
    },
}


class TestExistingCopyRefused:
    @pytest.mark.parametrize("tool", sorted(CONTENT_DOORS))
    def test_existing_copy_is_refused_like_an_unknown_alias(self, client, env, tool):
        """A copy activated before refusal existed (created the way the
        manager creates every activation) serves nothing to its owner."""
        env.activate_for(USER, CIDX_META_REPO, META_COPY)
        arguments = CONTENT_DOORS[tool]

        copy = _mcp(client, USER, tool, arguments(META_COPY))
        unknown = _mcp(client, USER, tool, arguments(UNKNOWN_ALIAS))

        assert _refusal(copy, META_COPY) == _refusal(unknown, UNKNOWN_ALIAS)

    def test_existing_copy_is_refused_to_an_impersonating_admin(self, client, env):
        env.activate_for(USER, CIDX_META_REPO, META_COPY)
        session_id = _impersonating(client, USER)

        body = _mcp(
            client,
            ADMIN,
            "get_file_content",
            CONTENT_DOORS["get_file_content"](META_COPY),
            session_id,
        )

        _refusal(body, META_COPY)

    def test_existing_copy_can_still_be_deactivated(self, client, env):
        env.activate_for(USER, CIDX_META_REPO, META_COPY)

        payload = _payload(
            _mcp(client, USER, "deactivate_repository", {"user_alias": META_COPY})
        )

        assert payload["success"] is True, payload


class TestUnchanged:
    def test_admins_own_copy_serves_the_catalog(self, client, env):
        env.activate_for(ADMIN, CIDX_META_REPO, ADMIN_META_COPY)

        body = _mcp(
            client,
            ADMIN,
            "get_file_content",
            CONTENT_DOORS["get_file_content"](ADMIN_META_COPY),
        )

        assert CATALOG_TEXT in _file_text(_payload(body))

    def test_shared_catalog_read_for_a_normal_user_is_unchanged(self, client, env):
        body = _mcp(
            client,
            USER,
            "get_file_content",
            {"repository_alias": META_GLOBAL, "file_path": README},
        )

        assert README_TEXT in _file_text(_payload(body))
