"""RED/GREEN tests for the Story #1593 group tool-access REST surface."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


def _admin_user() -> User:
    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


def _group(group_id: int, name: str = "developers"):
    return SimpleNamespace(id=group_id, name=name)


def _manager():
    manager = Mock()
    manager.get_all_groups.return_value = [_group(1), _group(2, "ops")]
    manager.get_group.side_effect = lambda group_id: (
        _group(group_id) if group_id in {1, 2} else None
    )
    manager.set_tool_access.return_value = True
    manager.set_tool_access_all_groups.return_value = [1, 2]
    manager.is_tool_allowed.return_value = False
    return manager


def test_get_lists_registered_tools_with_per_group_state(monkeypatch) -> None:
    from code_indexer.server.routers import groups

    monkeypatch.setattr(
        groups,
        "TOOL_REGISTRY",
        {
            "git_push": {"name": "git_push"},
            "authenticate": {"name": "authenticate"},
        },
        raising=False,
    )
    result = groups.get_tool_access(group_manager=_manager())

    names = {entry["tool_name"] for entry in result["tools"]}
    assert names == {"git_push", "authenticate"}
    assert all("groups" in entry for entry in result["tools"])


def test_grant_writes_and_audits_tool_mutation() -> None:
    from code_indexer.server.routers import groups

    manager = _manager()
    result = groups.grant_tool_access(
        group_id=1,
        tool_name="git_push",
        current_user=_admin_user(),
        group_manager=manager,
    )

    assert result["allowed"] is True
    manager.set_tool_access.assert_called_once_with("git_push", 1, True, "admin")
    manager.log_audit.assert_called_once()
    assert manager.log_audit.call_args.kwargs["target_type"] == "tool"
    assert manager.log_audit.call_args.kwargs["action_type"] == "tool_access_grant"


def test_revoke_writes_false_and_audits_tool_mutation() -> None:
    from code_indexer.server.routers import groups

    manager = _manager()
    result = groups.revoke_tool_access(
        group_id=1,
        tool_name="git_push",
        current_user=_admin_user(),
        group_manager=manager,
    )

    assert result["allowed"] is False
    manager.set_tool_access.assert_called_once_with("git_push", 1, False, "admin")
    assert manager.log_audit.call_args.kwargs["action_type"] == "tool_access_revoke"


def test_bulk_disable_is_one_fanout_and_audit_per_group() -> None:
    from code_indexer.server.routers import groups

    manager = _manager()
    result = groups.bulk_disable_tool_access(
        tool_name="git_push",
        current_user=_admin_user(),
        group_manager=manager,
    )

    assert result["affected_group_ids"] == [1, 2]
    manager.set_tool_access_all_groups.assert_called_once_with(
        "git_push", False, "admin"
    )
    assert manager.log_audit.call_count == 2
    assert all(
        call.kwargs["action_type"] == "tool_access_bulk_disable"
        for call in manager.log_audit.call_args_list
    )


def test_audit_failure_does_not_block_authoritative_mutation() -> None:
    from code_indexer.server.routers import groups

    manager = _manager()
    manager.log_audit.side_effect = RuntimeError("audit store unavailable")

    result = groups.revoke_tool_access(
        group_id=1,
        tool_name="git_push",
        current_user=_admin_user(),
        group_manager=manager,
    )

    assert result["allowed"] is False
    manager.set_tool_access.assert_called_once()


def test_authenticate_cannot_be_mutated() -> None:
    from code_indexer.server.routers import groups

    with pytest.raises(Exception, match="authenticate"):
        groups.revoke_tool_access(
            group_id=1,
            tool_name="authenticate",
            current_user=_admin_user(),
            group_manager=_manager(),
        )
