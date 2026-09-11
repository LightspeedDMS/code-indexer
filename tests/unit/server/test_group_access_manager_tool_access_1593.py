"""AC2 RED/GREEN tests for SQLite tool-group access methods."""

from pathlib import Path

import pytest

from code_indexer.server.services.group_access_manager import GroupAccessManager


@pytest.fixture
def manager(tmp_path: Path) -> GroupAccessManager:
    return GroupAccessManager(tmp_path / "groups.db")


def test_tool_access_upsert_and_fail_closed(manager: GroupAccessManager) -> None:
    group = manager.create_group("developers", "Developer group")

    assert manager.is_tool_allowed("git_push", group.id) is False
    assert manager.set_tool_access("git_push", group.id, True, "admin") is True
    assert manager.is_tool_allowed("git_push", group.id) is True
    assert manager.get_group_tools(group.id) == ["git_push"]
    assert [g.id for g in manager.get_tool_groups("git_push")] == [group.id]

    assert manager.set_tool_access("git_push", group.id, False, "admin") is True
    assert manager.is_tool_allowed("git_push", group.id) is False
    assert manager.get_group_tools(group.id) == []
    assert manager.get_tool_groups("git_push") == []


def test_tool_access_requires_existing_group(manager: GroupAccessManager) -> None:
    with pytest.raises(ValueError, match="not found"):
        manager.set_tool_access("git_push", 99999, True, "admin")


def test_atomic_fanout_rolls_back_on_mid_operation_failure(
    manager: GroupAccessManager,
) -> None:
    groups = [manager.create_group(f"group-{i}", "test") for i in range(3)]
    connection = manager._get_connection()
    # A trigger aborts after the first row, exercising the real transaction
    # rollback boundary without replacing sqlite3.Connection internals.
    connection.execute(
        """
        CREATE TRIGGER fail_tool_access AFTER INSERT ON tool_group_access
        WHEN (SELECT COUNT(*) FROM tool_group_access
              WHERE tool_name = NEW.tool_name) >= 1
        BEGIN SELECT RAISE(ABORT, 'injected fan-out failure'); END
        """
    )
    with pytest.raises(Exception, match="injected"):
        manager.set_tool_access_all_groups("git_push", True, "admin")

    for group in groups:
        assert manager.is_tool_allowed("git_push", group.id) is False


def test_atomic_fanout_returns_all_group_ids(manager: GroupAccessManager) -> None:
    groups = [manager.create_group(f"group-{i}", "test") for i in range(2)]
    affected = manager.set_tool_access_all_groups("search_code", True, "admin")
    admins = manager.get_group_by_name("admins")
    powerusers = manager.get_group_by_name("powerusers")
    users = manager.get_group_by_name("users")
    assert admins is not None
    assert powerusers is not None
    assert users is not None
    assert set(affected) == {g.id for g in groups} | {
        admins.id,
        powerusers.id,
        users.id,
    }
    assert all(manager.is_tool_allowed("search_code", group.id) for group in groups)
