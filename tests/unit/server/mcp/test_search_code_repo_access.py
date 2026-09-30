"""MCP search_code searches only repositories the caller can access.

Driven through the real MCP dispatcher (protocol.handle_tools_call) and the
real search_code handler, over the real SemanticQueryManager, global
registry, activation and AccessFilteringService from query_repo_access_env.
Only the external embedding + HNSW search boundary is replaced.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Set

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search import search_code
from code_indexer.server.mcp.protocol import handle_tools_call
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    ALL_REPOS,
    ALL_ROWS_LIMIT,
    GRANTED_REPO,
    GRANTED_REPOS,
    OWN_ACTIVATION,
    USER,
    QueryAccessEnv,
    global_alias,
)


def _user(username: str, role: UserRole) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


NON_ADMIN = _user(USER, UserRole.NORMAL_USER)
ADMIN_USER = _user(ADMIN, UserRole.ADMIN)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[QueryAccessEnv]:
    # No embedding provider key: deterministic primary-only routing and no
    # path can reach a real provider (the search boundary is faked).
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    e = QueryAccessEnv(tmp_path)
    try:
        yield e
    finally:
        e.close()


def _payload(mcp_result: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = json.loads(mcp_result["content"][0]["text"])
    return payload


async def _call(
    env: QueryAccessEnv,
    user: User,
    access_service: Optional[Any],
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    with env.installed(access_service):
        result = await handle_tools_call(
            {"name": "search_code", "arguments": arguments}, user
        )
    return _payload(result)


def _repos_of(payload: Dict[str, Any]) -> Set[str]:
    return {r["repository_alias"] for r in payload["results"]["results"]}


class TestSearchCodeWithoutAlias:
    async def test_non_admin_gets_granted_rows_and_metadata_only(self, env):
        payload = await _call(
            env,
            NON_ADMIN,
            env.access_service,
            {"query_text": "find", "limit": ALL_ROWS_LIMIT},
        )

        assert payload["success"] is True, payload
        assert _repos_of(payload) == {global_alias(r) for r in GRANTED_REPOS}
        metadata = payload["results"]["query_metadata"]
        assert metadata["repositories_searched"] == len(GRANTED_REPOS)

    async def test_admin_gets_every_global_repo(self, env):
        payload = await _call(
            env,
            ADMIN_USER,
            env.access_service,
            {"query_text": "find", "limit": ALL_ROWS_LIMIT},
        )

        assert payload["success"] is True, payload
        assert _repos_of(payload) == {global_alias(r) for r in ALL_REPOS}


class TestSearchCodeMissingAccessService:
    @pytest.mark.parametrize("user", [NON_ADMIN, ADMIN_USER], ids=["user", "admin"])
    async def test_no_alias_is_refused(self, env, user):
        payload = await _call(env, user, None, {"query_text": "find"})

        assert payload["success"] is False
        assert "access control" in payload["error"].lower()
        assert payload["results"] == []
        assert env.searched_paths == []

    async def test_explicit_global_alias_is_refused_by_the_dispatcher(self, env):
        with pytest.raises(ValueError, match="(?i)access control"):
            await _call(
                env,
                NON_ADMIN,
                None,
                {"query_text": "find", "repository_alias": global_alias(GRANTED_REPO)},
            )

        assert env.searched_paths == []

    async def test_own_activation_alias_is_refused_by_the_dispatcher(self, env):
        # Pre-existing Story #331 AC9 dispatcher guard: with no access
        # service, ANY call carrying a repository parameter is denied before
        # the handler runs.
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        with pytest.raises(ValueError, match="(?i)access control"):
            await _call(
                env,
                NON_ADMIN,
                None,
                {"query_text": "find", "repository_alias": OWN_ACTIVATION},
            )

    def test_own_activation_alias_runs_in_the_search_code_handler(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)

        # search_code is a synchronous handler (plain def); the dispatcher
        # runs it on a worker thread, so it is called directly here.
        with env.installed(None):
            payload = _payload(
                search_code(
                    {"query_text": "find", "repository_alias": OWN_ACTIVATION},
                    NON_ADMIN,
                )
            )

        assert payload["success"] is True, payload
        assert _repos_of(payload) == {OWN_ACTIVATION}
