"""Bug #2076: per-group tool grants must not report silent success.

Grants are stored but not enforced in this version (enforcement waits for a
readiness marker, ``tool_access_migration_state.complete``, that nothing in
this version writes). The REST surface must say so: every response carries
``enforced`` (read from the same readiness check MCP authorization uses),
writes log a WARNING while it is false, and the write itself is unchanged.

Uses a real GroupAccessManager on a throwaway SQLite database.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.routers import groups
from code_indexer.server.services.group_access_manager import GroupAccessManager

_TOOL = next(name for name in sorted(TOOL_REGISTRY) if name != "authenticate")


def _admin() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def manager(tmp_path: Path) -> GroupAccessManager:
    return GroupAccessManager(tmp_path / "groups.db")


@pytest.fixture
def group_id(manager: GroupAccessManager) -> int:
    return int(manager.get_all_groups()[0].id)


def _mark_enforcement_ready(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO tool_access_migration_state (id, complete) "
            "VALUES (1, 1)"
        )
        conn.commit()
    finally:
        conn.close()


def _break_readiness_marker(db_path: Path) -> None:
    """A marker table without its `complete` column: the real SQLite read
    raises OperationalError("no such column"), a genuine read failure as
    opposed to the marker table being absent."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE tool_access_migration_state (id INTEGER PRIMARY KEY)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("route", ["grant", "revoke", "bulk", "read"])
def test_failed_readiness_read_fails_the_request_and_writes_nothing(
    tmp_path, manager, group_id, route
) -> None:
    before = manager.is_tool_allowed(_TOOL, group_id)
    _break_readiness_marker(tmp_path / "groups.db")
    calls = {
        "grant": lambda: groups.grant_tool_access(
            group_id=group_id,
            tool_name=_TOOL,
            current_user=_admin(),
            group_manager=manager,
        ),
        "revoke": lambda: groups.revoke_tool_access(
            group_id=group_id,
            tool_name=_TOOL,
            current_user=_admin(),
            group_manager=manager,
        ),
        "bulk": lambda: groups.bulk_disable_tool_access(
            tool_name=_TOOL, current_user=_admin(), group_manager=manager
        ),
        "read": lambda: groups.get_tool_access(
            current_user=_admin(), group_manager=manager
        ),
    }

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        calls[route]()

    assert manager.is_tool_allowed(_TOOL, group_id) is before


def _not_enforced_warnings(caplog: pytest.LogCaptureFixture):
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "not enforced" in r.getMessage()
    ]


def _assert_not_enforced(result: dict) -> None:
    assert result["enforced"] is False
    assert "not enforced" in result["enforcement_note"]


def test_grant_reports_not_enforced_and_warns(manager, group_id, caplog) -> None:
    caplog.set_level(logging.WARNING)
    result = groups.grant_tool_access(
        group_id=group_id, tool_name=_TOOL, current_user=_admin(), group_manager=manager
    )

    _assert_not_enforced(result)
    assert result["allowed"] is True
    assert manager.is_tool_allowed(_TOOL, group_id) is True  # still stored
    assert len(_not_enforced_warnings(caplog)) == 1


def test_revoke_reports_not_enforced_and_warns(manager, group_id, caplog) -> None:
    caplog.set_level(logging.WARNING)
    result = groups.revoke_tool_access(
        group_id=group_id, tool_name=_TOOL, current_user=_admin(), group_manager=manager
    )

    _assert_not_enforced(result)
    assert result["allowed"] is False
    assert manager.is_tool_allowed(_TOOL, group_id) is False
    assert len(_not_enforced_warnings(caplog)) == 1


def test_bulk_disable_reports_not_enforced_and_warns(manager, caplog) -> None:
    caplog.set_level(logging.WARNING)
    result = groups.bulk_disable_tool_access(
        tool_name=_TOOL, current_user=_admin(), group_manager=manager
    )

    _assert_not_enforced(result)
    assert sorted(result["affected_group_ids"]) == sorted(
        g.id for g in manager.get_all_groups()
    )
    assert len(_not_enforced_warnings(caplog)) == 1


def test_read_reports_not_enforced(manager, caplog) -> None:
    caplog.set_level(logging.WARNING)
    result = groups.get_tool_access(current_user=_admin(), group_manager=manager)

    _assert_not_enforced(result)
    assert result["tools"]  # listing unchanged
    assert _not_enforced_warnings(caplog) == []  # reads do not warn


def test_enforced_follows_the_real_readiness_gate(
    tmp_path, manager, group_id, caplog
) -> None:
    _mark_enforcement_ready(tmp_path / "groups.db")
    caplog.set_level(logging.WARNING)

    write = groups.grant_tool_access(
        group_id=group_id, tool_name=_TOOL, current_user=_admin(), group_manager=manager
    )
    read = groups.get_tool_access(current_user=_admin(), group_manager=manager)

    assert write["enforced"] is True and "enforcement_note" not in write
    assert read["enforced"] is True and "enforcement_note" not in read
    assert _not_enforced_warnings(caplog) == []
