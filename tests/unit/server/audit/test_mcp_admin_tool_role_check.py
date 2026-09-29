"""MCP admin tools hold the admin role inside the handler.

With tool-access enforcement on, a group tool grant admits a caller to a tool
at the dispatcher and the tool's ``required_permission`` is not consulted.
Every MCP tool whose REST/Web twin is admin-only must therefore refuse a
non-admin in the handler itself, however the call was admitted: group
management (list/create/get/update/delete, members, repositories), user
list/create, the global refresh-interval setting, the memory-governor
snapshot, dependency-map triggering, SSH key management and the destructive
git tools (``git_reset``, ``git_clean``, ``git_branch_delete``, whose REST
twins require ``repository:admin``).

Harness: ``_audit_front_doors`` (real MCP JSON-RPC dispatcher, real group
store with tool-access enforcement switched on, real user manager and
config service; elevation enforcement off, so the role check is the only
gate under test).  The ``ssh_dir`` fixture puts the MCP SSH key manager on
this test's own temporary SSH directory, so no test reads the real
``~/.ssh`` even if a role check regressed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Tuple

import pytest

from _audit_front_doors import DoorsEnv, front_door_env
from _audit_repos_support import EXAMPLE_ALIAS
from code_indexer.server.auth.user_manager import User, UserRole

_ADMIN_ROLE_REQUIRED = {
    "success": False,
    "error": "Permission denied: admin role required",
}
_POWER_USER = "example-power-user"
_CUSTOM_GROUP = "example-custom-group"
_MEMBER = "example-member"
_JOINER = "example-joiner"
_OTHER_REPO = "example-other-repo"
_NEW_GROUP = "example-new-group"
_NEW_USER = "example-new-user"


_GLOBAL_ALIAS = f"{EXAMPLE_ALIAS}-global"
_UNLISTED_ALIAS = "example-unlisted-global"


@pytest.fixture()
def ssh_dir(tmp_path: Path, monkeypatch) -> Path:
    """The MCP SSH key manager on this test's own SSH directory."""
    from code_indexer.server.mcp.handlers import ssh_keys as mcp_ssh_keys
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager

    directory = tmp_path / "ssh"
    directory.mkdir(mode=0o700)
    manager = SSHKeyManager(
        ssh_dir=directory,
        metadata_dir=tmp_path / "ssh-meta",
        config_path=directory / "config",
    )
    monkeypatch.setattr(mcp_ssh_keys, "_ssh_key_manager", manager)
    return directory


@pytest.fixture()
def env(tmp_path: Path, monkeypatch, ssh_dir: Path) -> Iterator[DoorsEnv]:
    yield from front_door_env(tmp_path, monkeypatch)


def _groups() -> Any:
    from code_indexer.server.mcp.handlers._utils import app_module

    return app_module.app.state.group_manager


def _custom_group() -> Any:
    """A custom group with one member and one repository (created once)."""
    groups = _groups()
    group = groups.get_group_by_name(_CUSTOM_GROUP)
    if group is None:
        group = groups.create_group(_CUSTOM_GROUP, "")
        groups.assign_user_to_group(_MEMBER, group.id, "test-setup")
        groups.grant_repo_access(EXAMPLE_ALIAS, group.id, granted_by="test-setup")
    return group


def _grant(tool: str) -> User:
    """A power user admitted to *tool* by a group tool grant."""
    from code_indexer.server.services.constants import DEFAULT_GROUP_POWERUSERS

    groups = _groups()
    powerusers = groups.get_group_by_name(DEFAULT_GROUP_POWERUSERS)
    assert powerusers is not None
    groups.assign_user_to_group(_POWER_USER, powerusers.id, "test-setup")
    groups.grant_repo_access(EXAMPLE_ALIAS, powerusers.id, granted_by="test-setup")
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
        created_at=datetime.now(timezone.utc),
    )


def _jsonrpc(user: User, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    from code_indexer.server.mcp.protocol import process_jsonrpc_request

    response: Dict[str, Any] = asyncio.run(
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
    return response


def _call(user: User, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    response = _jsonrpc(user, tool, arguments)
    assert "result" in response, response
    result: Dict[str, Any] = response["result"]
    if "content" not in result:
        # A handler may return its snapshot unwrapped (memory governor).
        return result
    payload: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return payload


def _custom_group_unchanged(env: DoorsEnv) -> None:
    groups = _groups()
    group = groups.get_group_by_name(_CUSTOM_GROUP)
    assert group is not None
    assert groups.get_group_by_name(_NEW_GROUP) is None
    assert groups.get_users_in_group(group.id) == [_MEMBER]
    repos = groups.get_group_repos(group.id)
    assert EXAMPLE_ALIAS in repos
    assert _OTHER_REPO not in repos


def _no_new_group(env: DoorsEnv) -> None:
    assert _groups().get_group_by_name(_NEW_GROUP) is None


def _no_new_user(env: DoorsEnv) -> None:
    assert env.stack.user_manager.get_user(_NEW_USER) is None


def _nothing(env: DoorsEnv) -> None:
    return None


def _gid() -> str:
    return str(_custom_group().id)


_Case = Tuple[str, Callable[[], Dict[str, Any]], Callable[[DoorsEnv], None]]

_CASES: Dict[str, _Case] = {
    "list_groups": ("list_groups", lambda: {}, _nothing),
    "create_group": (
        "create_group",
        lambda: {"name": _NEW_GROUP, "description": "example"},
        _no_new_group,
    ),
    "get_group": ("get_group", lambda: {"group_id": _gid()}, _nothing),
    "update_group": (
        "update_group",
        lambda: {"group_id": _gid(), "name": _NEW_GROUP},
        _custom_group_unchanged,
    ),
    "delete_group": (
        "delete_group",
        lambda: {"group_id": _gid()},
        _custom_group_unchanged,
    ),
    "members_add": (
        "manage_group_members",
        lambda: {"action": "add", "group_id": _gid(), "user_id": _JOINER},
        _custom_group_unchanged,
    ),
    "members_remove": (
        "manage_group_members",
        lambda: {"action": "remove", "group_id": _gid(), "user_id": _MEMBER},
        _custom_group_unchanged,
    ),
    "repos_add": (
        "manage_group_repos",
        lambda: {"action": "add", "group_id": _gid(), "repos": [_OTHER_REPO]},
        _custom_group_unchanged,
    ),
    "repos_remove": (
        "manage_group_repos",
        lambda: {"action": "remove", "group_id": _gid(), "repos": [EXAMPLE_ALIAS]},
        _custom_group_unchanged,
    ),
    "repos_bulk_remove": (
        "manage_group_repos",
        lambda: {
            "action": "bulk_remove",
            "group_id": _gid(),
            "repos": [EXAMPLE_ALIAS],
        },
        _custom_group_unchanged,
    ),
    "list_users": ("list_users", lambda: {}, _nothing),
    "create_user": (
        "create_user",
        lambda: {
            "username": _NEW_USER,
            "password": "Example-Passw0rd!Xyz",
            "role": "admin",
        },
        _no_new_user,
    ),
    "get_memory_governor_stats": ("get_memory_governor_stats", lambda: {}, _nothing),
    "trigger_dependency_analysis": (
        "trigger_dependency_analysis",
        lambda: {"mode": "delta"},
        _nothing,
    ),
    "list_ssh_keys": ("list_ssh_keys", lambda: {}, _nothing),
    "manage_ssh_key": (
        "manage_ssh_key",
        lambda: {"action": "show_public", "name": "example-key"},
        _nothing,
    ),
    # No confirmation token: even an admitted call only returns a token.
    "git_reset": (
        "git_reset",
        lambda: {"repository_alias": _GLOBAL_ALIAS, "mode": "hard"},
        _nothing,
    ),
    "git_clean": ("git_clean", lambda: {"repository_alias": _GLOBAL_ALIAS}, _nothing),
    "git_branch_delete": (
        "git_branch_delete",
        lambda: {"repository_alias": _GLOBAL_ALIAS, "branch_name": "example-branch"},
        _nothing,
    ),
}

_GIT_TOOLS: Dict[str, Dict[str, Any]] = {
    "git_reset": {"mode": "hard"},
    "git_clean": {},
    "git_branch_delete": {"branch_name": "example-branch"},
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_granted_non_admin_is_refused_by_the_handler(env, case) -> None:
    tool, arguments, check = _CASES[case]
    args = arguments()
    user = _grant(tool)
    assert _call(user, tool, args) == _ADMIN_ROLE_REQUIRED
    check(env)


def test_granted_non_admin_cannot_set_the_global_refresh_interval(env) -> None:
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations
    from code_indexer.server.mcp.handlers._utils import _get_golden_repos_dir

    ops = GlobalRepoOperations(_get_golden_repos_dir())
    before = ops.get_config()["refresh_interval"]
    user = _grant("set_global_config")
    result = _call(user, "set_global_config", {"refresh_interval": before + 60})
    assert result == _ADMIN_ROLE_REQUIRED
    assert ops.get_config()["refresh_interval"] == before


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("list_groups", {}),
        ("create_group", {"name": _NEW_GROUP}),
        ("list_users", {}),
        ("get_memory_governor_stats", {}),
    ],
    ids=["list_groups", "create_group", "list_users", "memory_governor"],
)
def test_admin_is_unaffected(env, tool, arguments) -> None:
    result = _call(env.admin, tool, arguments)
    assert result != _ADMIN_ROLE_REQUIRED
    assert result.get("success", True) is True, result
    if tool == "create_group":
        assert _groups().get_group_by_name(_NEW_GROUP) is not None


@pytest.mark.parametrize("tool", sorted(_GIT_TOOLS))
def test_repository_access_guard_still_refuses_first(env, tool) -> None:
    """A granted non-admin naming a repository outside its groups is refused
    by the dispatcher's repository guard, before any handler runs."""
    user = _grant(tool)
    arguments = {"repository_alias": _UNLISTED_ALIAS, **_GIT_TOOLS[tool]}
    response = _jsonrpc(user, tool, arguments)
    assert "result" not in response, response
    assert "access" in response["error"]["message"].lower(), response


@pytest.mark.parametrize("tool", sorted(_GIT_TOOLS))
def test_admin_reaches_the_git_handler(env, tool) -> None:
    """The admin passes the role check and gets the handler's own answer:
    the harness repository is a local one, which the handler's path
    resolution refuses for git operations (so nothing is changed)."""
    result = _call(
        env.admin, tool, {"repository_alias": _GLOBAL_ALIAS, **_GIT_TOOLS[tool]}
    )
    assert result == {
        "success": False,
        "error": f"Repository '{_GLOBAL_ALIAS}' is a local repository "
        "and does not support git operations.",
    }


_ELEVATION_GATED_READS: Dict[str, Dict[str, Any]] = {
    "admin_logs_query": {},
    "admin_logs_export": {"format": "json"},
    "query_audit_logs": {},
}
_ELEVATION_ERRORS = {"elevation_required", "totp_setup_required"}


@pytest.mark.parametrize("tool", sorted(_ELEVATION_GATED_READS))
def test_role_denial_comes_before_the_elevation_gate(env, tool) -> None:
    """With elevation enforced, a granted non-admin is refused for its role,
    not asked to elevate (the role check is the outermost gate)."""
    from tests.unit.server.self_service_elevation_harness import enforcement

    user = _grant(tool)
    with enforcement(True):
        result = _call(user, tool, _ELEVATION_GATED_READS[tool])
    assert result == _ADMIN_ROLE_REQUIRED


@pytest.mark.parametrize("tool", sorted(_ELEVATION_GATED_READS))
def test_admin_still_meets_the_elevation_gate(env, tool) -> None:
    """The elevation requirement is unchanged for an admin without a window."""
    from tests.unit.server.self_service_elevation_harness import enforcement

    with enforcement(True):
        result = _call(env.admin, tool, _ELEVATION_GATED_READS[tool])
    assert result.get("error") in _ELEVATION_ERRORS, result


@pytest.mark.parametrize("tool", sorted(_ELEVATION_GATED_READS))
def test_elevation_gated_reads_keep_the_session_key_marker(tool) -> None:
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

    assert getattr(HANDLER_REGISTRY[tool], "__mcp_requires_session_key__", False)


def test_admin_ssh_listing_reads_only_the_test_ssh_directory(env, ssh_dir) -> None:
    (ssh_dir / "id_example").write_text("not a real key\n")
    result = _call(env.admin, "list_ssh_keys", {})
    assert result["success"] is True, result
    listed = json.dumps(result)
    assert str(Path.home() / ".ssh") not in listed
    assert result["managed"] == []
