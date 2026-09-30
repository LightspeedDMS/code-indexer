"""
MCP ``get_provider_health`` follows the role rule of its REST twins.

REST ``GET /api/admin/provider-indexes/health`` and
``GET /admin/provider-health`` both depend on
``get_current_admin_user_hybrid`` (admin only, no elevation). The MCP tool is
admin-only at the dispatcher and in the handler itself, so a non-admin
admitted by a group tool grant is refused too.

Every call goes through the real MCP ``tools/call`` dispatcher
(``handle_tools_call``) with a real ``GroupAccessManager`` and the real
``ProviderHealthMonitor``.
"""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

from code_indexer.server import app as real_app_module
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import handle_tools_call
from code_indexer.server.mcp.tools import filter_tools_by_role
from code_indexer.server.services.constants import DEFAULT_GROUP_POWERUSERS
from code_indexer.server.services.group_access_manager import GroupAccessManager

_TOOL = "get_provider_health"


def _user(username: str, role: UserRole) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


ADMIN = _user("ops-admin", UserRole.ADMIN)
POWER_USER = _user("builder", UserRole.POWER_USER)
NORMAL_USER = _user("reader", UserRole.NORMAL_USER)


@pytest.fixture
def granted_group_manager(tmp_path: Path) -> GroupAccessManager:
    """Group tool-access enforcement on; powerusers granted the tool."""
    db_path = tmp_path / "groups.db"
    manager = GroupAccessManager(db_path)
    powerusers = manager.get_group_by_name(DEFAULT_GROUP_POWERUSERS)
    assert powerusers is not None
    manager.assign_user_to_group(POWER_USER.username, powerusers.id, "test")
    manager.set_tool_access(_TOOL, powerusers.id, True, "test")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT INTO tool_access_migration_state (id, complete) VALUES (1, 1)"
        )
        conn.commit()
    return manager


async def _call(
    user: User, tool_access_manager: Optional[GroupAccessManager] = None
) -> Dict[str, Any]:
    with (
        patch.object(
            real_app_module.app.state,
            "group_manager",
            tool_access_manager,
            create=True,
        ),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
    ):
        response = await handle_tools_call({"name": _TOOL, "arguments": {}}, user)
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [POWER_USER, NORMAL_USER], ids=["power", "normal"])
async def test_non_admin_is_refused_by_dispatcher(user):
    with pytest.raises(ValueError, match="Permission denied"):
        await _call(user)


def test_tool_is_listed_for_admin_only():
    assert _TOOL in {tool["name"] for tool in filter_tools_by_role(ADMIN)}
    for user in (POWER_USER, NORMAL_USER):
        assert _TOOL not in {tool["name"] for tool in filter_tools_by_role(user)}


@pytest.mark.asyncio
async def test_group_granted_power_user_is_refused_by_handler(granted_group_manager):
    data = await _call(POWER_USER, granted_group_manager)
    assert data == {"success": False, "error": "Permission denied: admin role required"}


@pytest.mark.asyncio
async def test_admin_gets_provider_health():
    data = await _call(ADMIN)
    assert data.get("success") is True
    assert isinstance(data.get("provider_health"), dict)
