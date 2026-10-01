"""
AC8 integration tests: groupless and orphaned users fail closed at all
five real MCP enforcement sites (Story #1593).

These tests call the five real enforcement-site functions directly
(`filter_tools_by_role`, `handle_tools_list`, `handle_public_tools_list`,
`handle_tools_call`, `quick_reference`, `get_tool_categories`) against a
real `GroupAccessManager` (SQLite) with the AC9 readiness marker set
True -- no mocks of the enforcement decision itself. This is the
integration coverage AC3's own testing requirements call for ("All five
enforcement sites hide or reject a disallowed tool") that was not
present after AC3/AC4: only the isolated `tool_access.py` decision-engine
unit tests existed (`test_tool_access_decision_1593.py`), not tests
exercising the wiring at each real site.

Two principals under test, parametrized across every site below:
- A never-assigned "groupless" user: no `user_group_membership` row at all.
- An "orphaned" user: has a `user_group_membership` row, but the group
  row itself is deleted directly via raw SQL, bypassing
  `delete_group()`'s member-count guard -- simulating the data-integrity
  edge case AC8's own gherkin describes ("a user whose group was deleted
  while they were still a member"). `get_user_group()`'s INNER JOIN
  naturally returns nothing once the group row is gone, so no special
  orphan-detection code is required for this to fail closed identically
  to the never-assigned case -- that identity is exactly what these
  tests prove.

TDD note: these are new integration coverage for an already
partly-implemented decision engine (AC3/AC4 landed last turn). Every
assertion here genuinely exercises the DENY path of REAL site code (not
a trivially-true allow case), and was run and confirmed passing on first
write against the current AC3/AC4 implementation -- serving as the
independent verification this pair-programming step requires, not a
classic pre-implementation RED (the implementation under test already
exists from the prior turn).
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp import protocol as protocol_module
from code_indexer.server.mcp.handlers import guides as guides_module
from code_indexer.server.mcp.tool_access import ToolAccessMemo
from code_indexer.server.mcp.tools import TOOL_REGISTRY, filter_tools_by_role
from code_indexer.server.services.group_access_manager import GroupAccessManager

_NON_AUTH_TOOL = next(name for name in TOOL_REGISTRY if name != "authenticate")


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    manager = GroupAccessManager(temp_db_path)
    # AC9: enforcement only fail-closes once the readiness marker is true.
    with sqlite3.connect(str(temp_db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT INTO tool_access_migration_state (id, complete) VALUES (1, 1)"
        )
        conn.commit()
    return manager


def _make_groupless_user(group_manager, temp_db_path, username: str) -> User:
    return User(
        username=username,
        password_hash="x",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


def _make_orphaned_user(group_manager, temp_db_path, username: str) -> User:
    """A user with a dangling user_group_membership row whose target
    group row was deleted directly via raw SQL -- bypassing
    delete_group()'s member-count guard, simulating the AC8 edge case."""
    custom_group = group_manager.create_group(
        f"orphan-source-{username}", "temp group, deleted out-of-band"
    )
    group_manager.assign_user_to_group(username, custom_group.id, assigned_by="admin")

    with sqlite3.connect(str(temp_db_path)) as conn:
        conn.execute("DELETE FROM groups WHERE id = ?", (custom_group.id,))
        conn.commit()

    # Confirm the simulated corruption: membership row survives, group is gone.
    assert group_manager.get_user_group(username) is None
    return User(
        username=username,
        password_hash="x",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


# Parametrized over both fail-closed principals, so every site below is
# exercised for the never-assigned case AND the group-deleted-out-of-band
# case with no duplicated test bodies.
_PRINCIPAL_FACTORIES = {
    "groupless": _make_groupless_user,
    "orphaned": _make_orphaned_user,
}


@pytest.fixture(params=["groupless", "orphaned"])
def fail_closed_user(request, group_manager, temp_db_path):
    factory = _PRINCIPAL_FACTORIES[request.param]
    return factory(group_manager, temp_db_path, f"{request.param}-user")


def _assert_only_authenticate(names) -> None:
    names = set(names)
    assert names == {"authenticate"}, names


def _assert_non_auth_tool_hidden(rendered: str) -> None:
    assert _NON_AUTH_TOOL not in rendered, rendered


def _impersonating_admin(group_manager):
    """Return an admin with a grant while effective_user is groupless."""
    group = group_manager.create_group("impersonation-source", "test")
    group_manager.assign_user_to_group("raw-admin", group.id, assigned_by="system")
    group_manager.set_tool_access(_NON_AUTH_TOOL, group.id, True, "system")
    admin = User(
        username="raw-admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )
    effective = User(
        username="impersonated-groupless",
        password_hash="x",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )

    class Session:
        is_impersonating = True
        effective_user = effective

    return admin, Session()


def _unwrap_mcp_response(raw: dict) -> dict:
    """quick_reference() (like the MCP admin handlers) wraps its payload
    via _mcp_response(): the real dict is JSON-stringified inside
    raw["content"][0]["text"] per the MCP content-array protocol."""
    return json.loads(raw["content"][0]["text"])  # type: ignore[no-any-return]


class TestSite1FilterToolsByRole:
    def test_only_authenticate_visible(self, group_manager, fail_closed_user):
        memo = ToolAccessMemo(group_manager)
        tools = filter_tools_by_role(
            fail_closed_user, config=None, tool_access_memo=memo
        )
        _assert_only_authenticate(t["name"] for t in tools)


class TestSite2HandleToolsList:
    def test_only_authenticate_visible(self, group_manager, fail_closed_user):
        memo = ToolAccessMemo(group_manager)
        result = protocol_module.handle_tools_list(
            {}, fail_closed_user, tool_access_memo=memo
        )
        _assert_only_authenticate(t["name"] for t in result["tools"])

    def test_public_tools_list_only_authenticate_visible(
        self, group_manager, fail_closed_user
    ):
        memo = ToolAccessMemo(group_manager)
        result = protocol_module.handle_public_tools_list(
            fail_closed_user, tool_access_memo=memo
        )
        _assert_only_authenticate(t["name"] for t in result["tools"])


class TestSite3HandleToolsCall:
    async def test_denied_non_authenticate_tool(self, group_manager, fail_closed_user):
        memo = ToolAccessMemo(group_manager)

        with pytest.raises(ValueError, match="Permission denied"):
            await protocol_module.handle_tools_call(
                {"name": _NON_AUTH_TOOL, "arguments": {}},
                fail_closed_user,
                tool_access_memo=memo,
            )


class TestSite4QuickReference:
    def test_catalog_excludes_non_authenticate_tool(
        self, group_manager, fail_closed_user
    ):
        memo = ToolAccessMemo(group_manager)
        raw = guides_module.quick_reference({}, fail_closed_user, tool_access_memo=memo)
        result = _unwrap_mcp_response(raw)
        assert result["success"] is True, result
        _assert_non_auth_tool_hidden(str(result))

    def test_denied_direct_tool_lookup(self, group_manager, fail_closed_user):
        memo = ToolAccessMemo(group_manager)
        raw = guides_module.quick_reference(
            {"tool": _NON_AUTH_TOOL}, fail_closed_user, tool_access_memo=memo
        )
        result = _unwrap_mcp_response(raw)
        assert result["success"] is False, result


class TestSite5GetToolCategories:
    def test_categories_exclude_non_authenticate_tool(
        self, group_manager, fail_closed_user
    ):
        memo = ToolAccessMemo(group_manager)
        result = guides_module.get_tool_categories(
            {}, fail_closed_user, tool_access_memo=memo
        )
        _assert_non_auth_tool_hidden(str(result))


class TestEffectiveUserAtAllFiveSites:
    def test_filter_tools_uses_impersonated_user(self, group_manager):
        admin, session = _impersonating_admin(group_manager)
        result = filter_tools_by_role(
            admin,
            config=None,
            session_state=session,
            tool_access_memo=ToolAccessMemo(group_manager),
        )
        _assert_only_authenticate(t["name"] for t in result)

    def test_handle_tools_list_uses_impersonated_user(self, group_manager):
        admin, session = _impersonating_admin(group_manager)
        result = protocol_module.handle_tools_list(
            {},
            admin,
            session_state=session,
            tool_access_memo=ToolAccessMemo(group_manager),
        )
        _assert_only_authenticate(t["name"] for t in result["tools"])

    def test_public_tools_list_uses_impersonated_user(self, group_manager):
        admin, session = _impersonating_admin(group_manager)
        result = protocol_module.handle_public_tools_list(
            admin,
            session_state=session,
            tool_access_memo=ToolAccessMemo(group_manager),
        )
        _assert_only_authenticate(t["name"] for t in result["tools"])

    async def test_dispatch_uses_impersonated_user(self, group_manager):
        admin, session = _impersonating_admin(group_manager)

        with pytest.raises(ValueError, match="Permission denied"):
            await protocol_module.handle_tools_call(
                {"name": _NON_AUTH_TOOL, "arguments": {}},
                admin,
                session_state=session,
                tool_access_memo=ToolAccessMemo(group_manager),
            )

    def test_guides_use_impersonated_user(self, group_manager):
        admin, session = _impersonating_admin(group_manager)
        quick = guides_module.quick_reference(
            {},
            admin,
            session_state=session,
            tool_access_memo=ToolAccessMemo(group_manager),
        )
        categories = guides_module.get_tool_categories(
            {},
            admin,
            session_state=session,
            tool_access_memo=ToolAccessMemo(group_manager),
        )
        _assert_non_auth_tool_hidden(str(quick))
        _assert_non_auth_tool_hidden(str(categories))
