"""
AC8 regression pin: delete_group() refuses to delete a group that still
has members, on BOTH backends, so no user is ever orphaned into the
fail-closed-denied-by-default state (Story #1593).

This is a REGRESSION PIN, not a RED-then-GREEN discovery test: both
`GroupAccessManager.delete_group()` (SQLite) and
`GroupsPostgresBackend.delete_group()` (PostgreSQL) already raise
`GroupHasUsersError` when a group has members -- this is pre-existing,
verified behavior (Story #709), not new for #1593. What #1593 adds is
the STAKES: once AC3/AC4's fail-closed enforcement is live, a user with
no group is denied every tool except `authenticate`. A future change
that relaxed this guard (e.g. "helpfully" auto-reassigning orphaned
members, or silently allowing the delete) would silently strand real
users. This file pins the guard AND, unlike the pre-existing Story #709
coverage (`test_story_709_delete.py::test_delete_group_with_users_
service_layer`, which only asserts the exception type/message), also
explicitly asserts the membership and the group both survive the failed
attempt untouched -- proving "no member is left without a group" is
actually true, not just that an exception was raised. Both backends
raise the SAME `GroupHasUsersError` class (GroupsPostgresBackend imports
it from group_access_manager.py rather than defining its own), so both
tests below assert that exact type.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.group_access_manager import (
    GroupAccessManager,
    GroupHasUsersError,
)
from code_indexer.server.storage.postgres.groups_backend import GroupsPostgresBackend


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


class TestSQLiteDeleteGroupWithMembersRefused:
    def test_group_and_membership_survive_the_refused_delete(self, temp_db_path):
        manager = GroupAccessManager(temp_db_path)
        group = manager.create_group("ac8-pin-sqlite", "regression pin")
        manager.assign_user_to_group("ac8-pin-user", group.id, assigned_by="admin")

        with pytest.raises(GroupHasUsersError):
            manager.delete_group(group.id)

        # No member is left without a group: both the group and the
        # membership row must be completely untouched by the refused delete.
        assert manager.get_group(group.id) is not None
        surviving_membership = manager.get_user_group("ac8-pin-user")
        assert surviving_membership is not None
        assert surviving_membership.id == group.id


class TestPostgresDeleteGroupWithMembersRefused:
    def _pool_with_group_and_member(self, group_id: int):
        cursor = MagicMock()
        # First SELECT: the group row itself (is_default=False, non-null).
        # Second SELECT: COUNT(*) FROM user_group_membership -> 1 member.
        cursor.fetchone.side_effect = [
            {
                "id": group_id,
                "name": "ac8-pin-pg",
                "description": "regression pin",
                "is_default": False,
                "created_at": datetime.now(timezone.utc),
            },
            {"cnt": 1},
        ]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        pool = MagicMock()
        pool.connection.return_value.__enter__.return_value = connection
        return pool, connection, cursor

    def test_raises_before_any_delete_statement_executes(self):
        group_id = 42
        pool, connection, cursor = self._pool_with_group_and_member(group_id)
        backend = GroupsPostgresBackend(pool)

        with pytest.raises(GroupHasUsersError):
            backend.delete_group(group_id)

        # No member is left without a group: prove no DELETE statement
        # was ever issued -- the guard must short-circuit before any
        # mutation, not merely raise after a partial delete.
        executed_sql = " ".join(
            str(call.args[0]) for call in cursor.execute.call_args_list
        )
        assert "DELETE" not in executed_sql.upper(), executed_sql
        connection.commit.assert_not_called()
