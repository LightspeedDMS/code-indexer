"""MCP golden-repo add, remove and refresh are admin-only inside the handler.

A group tool grant can admit a non-admin to a tool at the dispatcher; the
handler must still refuse unless the caller holds the admin role, matching
the admin-only REST and Web twins (and the provider-index MCP handlers).

Harness: ``_audit_front_doors`` (real dispatcher, real group store with tool
access enforcement switched on, real managers).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from _audit_front_doors import DoorsEnv, front_door_env
from _audit_repos_support import EXAMPLE_ALIAS
from code_indexer.server.auth.user_manager import User, UserRole

_ADMIN_ROLE_REQUIRED = "Permission denied: admin role required"
_POWER_USER = "example-power-user"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    yield from front_door_env(tmp_path, monkeypatch)


def _grant(tool: str) -> User:
    """A power user admitted to *tool* by a group grant (and to the repo)."""
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.services.constants import DEFAULT_GROUP_POWERUSERS

    groups = app_module.app.state.group_manager
    powerusers = groups.get_group_by_name(DEFAULT_GROUP_POWERUSERS)
    assert powerusers is not None
    groups.assign_user_to_group(_POWER_USER, powerusers.id, "test-setup")
    for alias in (EXAMPLE_ALIAS, "example-new"):
        groups.grant_repo_access(alias, powerusers.id, granted_by="test-setup")
    with sqlite3.connect(str(groups.db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO tool_access_migration_state (id, complete) "
            "VALUES (1, 1)"
        )
        conn.commit()
    groups.set_tool_access(tool, powerusers.id, True, "test-setup")
    return User(
        username=_POWER_USER,
        password_hash="unused",
        role=UserRole.POWER_USER,
        created_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ),
    )


def _call(user: User, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    from code_indexer.server.mcp.protocol import process_jsonrpc_request

    response = asyncio.run(
        process_jsonrpc_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            user,
        )
    )
    assert "result" in response, response
    payload: Dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
    return payload


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("add_golden_repo", {"alias": "example-new"}),
        ("remove_golden_repo", {"alias": EXAMPLE_ALIAS}),
        ("refresh_golden_repo", {"alias": EXAMPLE_ALIAS}),
        ("change_golden_repo_branch", {"alias": EXAMPLE_ALIAS, "branch": "dev"}),
    ],
    ids=["add", "remove", "refresh", "change_branch"],
)
def test_granted_non_admin_is_refused_by_the_handler(env, tool, arguments) -> None:
    user = _grant(tool)
    if tool == "add_golden_repo":
        arguments = {**arguments, "url": env.git_url}
    result = _call(user, tool, arguments)
    assert result == {"success": False, "error": _ADMIN_ROLE_REQUIRED}
    assert env.jobs.submissions == []
    assert env.rows("golden_repo_") == []
