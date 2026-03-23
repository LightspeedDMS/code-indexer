"""
Unit tests for GroupsPostgresBackend.

Story #415: PostgreSQL GroupsDB Backend Migration

All tests mock the ConnectionPool — no real PostgreSQL required.
The mock hierarchy is:
    pool.connection() -> context manager -> conn
    conn.cursor()     -> context manager -> cur
    cur.execute(sql, params)
    cur.fetchone() / cur.fetchall()
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(fetchone=None, fetchall=None, rowcount=0):
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall if fetchall is not None else []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    pool = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    return pool, conn, cur


def _make_group_row(
    group_id=1,
    name="developers",
    description="Dev team",
    is_default=False,
    created_at=None,
):
    """Return a dict matching the groups table column structure."""
    return {
        "id": group_id,
        "name": name,
        "description": description,
        "is_default": is_default,
        "created_at": created_at or datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_isinstance_groups_backend_protocol(self):
        """
        Given a GroupsPostgresBackend instance
        When checked against GroupsBackend Protocol
        Then isinstance() returns True.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )
        from code_indexer.server.storage.protocols import GroupsBackend

        pool, _, _ = _make_pool()
        backend = GroupsPostgresBackend(pool)

        assert isinstance(backend, GroupsBackend)

    def test_all_protocol_methods_present(self):
        """
        Given a GroupsPostgresBackend instance
        When inspected for required Protocol methods
        Then all methods defined in GroupsBackend Protocol are callable.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, _ = _make_pool()
        backend = GroupsPostgresBackend(pool)

        required_methods = [
            "get_all_groups",
            "get_group",
            "get_group_by_name",
            "create_group",
            "update_group",
            "delete_group",
            "assign_user_to_group",
            "remove_user_from_group",
            "get_user_group",
            "get_user_membership",
            "get_users_in_group",
            "get_user_count_in_group",
            "grant_repo_access",
            "revoke_repo_access",
            "get_group_repos",
            "get_repo_groups",
            "get_repo_access",
            "auto_assign_golden_repo",
            "get_all_users_with_groups",
            "user_exists",
            "log_audit",
            "get_audit_logs",
        ]
        for method_name in required_methods:
            assert callable(
                getattr(backend, method_name, None)
            ), f"Missing or non-callable method: {method_name}"


# ---------------------------------------------------------------------------
# get_all_groups
# ---------------------------------------------------------------------------


class TestGetAllGroups:
    def test_get_all_groups_returns_list(self):
        """
        Given two group rows
        When get_all_groups() is called
        Then it returns a list of Group objects.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        rows = [
            _make_group_row(group_id=1, name="admins", is_default=True),
            _make_group_row(group_id=2, name="devs", is_default=False),
        ]
        pool, _, cur = _make_pool(fetchall=rows)
        backend = GroupsPostgresBackend(pool)

        result = backend.get_all_groups()

        assert isinstance(result, list)
        assert len(result) == 2

    def test_get_all_groups_executes_select_ordered(self):
        """
        Given a mocked pool
        When get_all_groups() is called
        Then it executes a SELECT with ORDER BY is_default DESC, name ASC.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool(fetchall=[])
        backend = GroupsPostgresBackend(pool)

        backend.get_all_groups()

        sql = cur.execute.call_args[0][0]
        assert "ORDER BY is_default DESC" in sql
        assert "name ASC" in sql

    def test_get_all_groups_returns_empty_list_when_no_rows(self):
        """
        Given no rows in the database
        When get_all_groups() is called
        Then it returns an empty list.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, _ = _make_pool(fetchall=[])
        backend = GroupsPostgresBackend(pool)

        result = backend.get_all_groups()

        assert result == []


# ---------------------------------------------------------------------------
# get_group
# ---------------------------------------------------------------------------


class TestGetGroup:
    def test_get_group_returns_group_when_found(self):
        """
        Given a group row with id=1
        When get_group(1) is called
        Then it returns a Group with that id.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        row = _make_group_row(group_id=1, name="devs")
        pool, _, cur = _make_pool(fetchone=row)
        backend = GroupsPostgresBackend(pool)

        result = backend.get_group(1)

        assert result is not None
        assert result.id == 1
        assert result.name == "devs"

    def test_get_group_returns_none_when_not_found(self):
        """
        Given no matching row
        When get_group() is called
        Then None is returned.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, _ = _make_pool(fetchone=None)
        backend = GroupsPostgresBackend(pool)

        result = backend.get_group(999)

        assert result is None

    def test_get_group_uses_parameterized_query(self):
        """
        Given a group lookup by id
        When get_group() is called
        Then the SQL uses a %s placeholder, not string interpolation.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool(fetchone=None)
        backend = GroupsPostgresBackend(pool)

        backend.get_group(42)

        sql, params = cur.execute.call_args[0]
        assert "%s" in sql
        assert 42 in params


# ---------------------------------------------------------------------------
# get_group_by_name
# ---------------------------------------------------------------------------


class TestGetGroupByName:
    def test_get_group_by_name_returns_group_when_found(self):
        """
        Given a group row with name='devs'
        When get_group_by_name('devs') is called
        Then it returns that Group.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        row = _make_group_row(group_id=2, name="devs")
        pool, _, _ = _make_pool(fetchone=row)
        backend = GroupsPostgresBackend(pool)

        result = backend.get_group_by_name("devs")

        assert result is not None
        assert result.name == "devs"

    def test_get_group_by_name_uses_case_insensitive_comparison(self):
        """
        Given a name query
        When get_group_by_name() is called
        Then the SQL uses LOWER() for case-insensitive matching.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool(fetchone=None)
        backend = GroupsPostgresBackend(pool)

        backend.get_group_by_name("Devs")

        sql = cur.execute.call_args[0][0]
        assert "LOWER" in sql


# ---------------------------------------------------------------------------
# create_group
# ---------------------------------------------------------------------------


class TestCreateGroup:
    def test_create_group_raises_when_name_exists(self):
        """
        Given an existing group with the same name
        When create_group() is called
        Then ValueError is raised.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        # First fetchone returns existing group (name check), second not needed
        existing = {"id": 1}
        pool, _, cur = _make_pool(fetchone=existing)
        backend = GroupsPostgresBackend(pool)

        try:
            backend.create_group("devs", "Dev team")
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "already exists" in str(exc)

    def test_create_group_executes_insert(self):
        """
        Given no existing group with that name
        When create_group() is called
        Then an INSERT INTO groups statement is executed.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        new_id_row = {"id": 3}
        created_row = _make_group_row(group_id=3, name="ops")

        pool, _, cur = _make_pool()
        # First call: name-check SELECT returns None (no duplicate)
        # Second call: INSERT RETURNING returns new_id_row
        # Third call: SELECT by id returns created_row
        cur.fetchone.side_effect = [None, new_id_row, created_row]
        backend = GroupsPostgresBackend(pool)

        _ = backend.create_group("ops", "Operations team")

        # Verify an INSERT was executed
        all_sqls = [call[0][0] for call in cur.execute.call_args_list]
        assert any("INSERT INTO groups" in sql for sql in all_sqls)


# ---------------------------------------------------------------------------
# assign_user_to_group
# ---------------------------------------------------------------------------


class TestAssignUserToGroup:
    def test_assign_user_executes_upsert(self):
        """
        Given a user and group id
        When assign_user_to_group() is called
        Then an INSERT ... ON CONFLICT DO UPDATE statement is executed.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool()
        backend = GroupsPostgresBackend(pool)

        backend.assign_user_to_group("alice", 1, "admin")

        sql = cur.execute.call_args[0][0]
        assert "INSERT INTO user_group_membership" in sql
        assert "ON CONFLICT" in sql

    def test_assign_user_uses_parameterized_query(self):
        """
        Given user_id, group_id, assigned_by
        When assign_user_to_group() is called
        Then params include all three values.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool()
        backend = GroupsPostgresBackend(pool)

        backend.assign_user_to_group("alice", 5, "bob")

        _, params = cur.execute.call_args[0]
        assert "alice" in params
        assert 5 in params
        assert "bob" in params


# ---------------------------------------------------------------------------
# remove_user_from_group
# ---------------------------------------------------------------------------


class TestRemoveUserFromGroup:
    def test_remove_user_executes_delete(self):
        """
        Given user_id and group_id
        When remove_user_from_group() is called
        Then a DELETE FROM user_group_membership statement is executed.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool()
        backend = GroupsPostgresBackend(pool)

        result = backend.remove_user_from_group("alice", 1)

        sql = cur.execute.call_args[0][0]
        assert "DELETE FROM user_group_membership" in sql
        assert result is True

    def test_remove_user_uses_parameterized_query(self):
        """
        Given user_id and group_id
        When remove_user_from_group() is called
        Then params contain both values (no string interpolation).
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool()
        backend = GroupsPostgresBackend(pool)

        backend.remove_user_from_group("carol", 7)

        sql, params = cur.execute.call_args[0]
        assert "%s" in sql
        assert "carol" in params
        assert 7 in params


# ---------------------------------------------------------------------------
# log_audit
# ---------------------------------------------------------------------------


class TestLogAudit:
    def test_log_audit_executes_insert(self):
        """
        Given audit event parameters
        When log_audit() is called
        Then an INSERT INTO audit_logs statement is executed.
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool()
        backend = GroupsPostgresBackend(pool)

        backend.log_audit(
            admin_id="admin",
            action_type="user_created",
            target_type="user",
            target_id="alice",
            details="some detail",
        )

        all_sqls = [call[0][0] for call in cur.execute.call_args_list]
        assert any("INSERT INTO audit_logs" in sql for sql in all_sqls)

    def test_log_audit_uses_parameterized_query(self):
        """
        Given audit event parameters
        When log_audit() is called
        Then the SQL uses %s placeholders (not f-string interpolation).
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        pool, _, cur = _make_pool()
        backend = GroupsPostgresBackend(pool)

        backend.log_audit(
            admin_id="admin",
            action_type="group_deleted",
            target_type="group",
            target_id="oldgroup",
        )

        all_calls = cur.execute.call_args_list
        insert_calls = [c for c in all_calls if "INSERT INTO audit_logs" in c[0][0]]
        assert len(insert_calls) == 1
        sql, params = insert_calls[0][0]
        assert "%s" in sql
        assert "admin" in params
        assert "group_deleted" in params


# ---------------------------------------------------------------------------
# get_audit_logs
# ---------------------------------------------------------------------------


class TestGetAuditLogs:
    def test_get_audit_logs_returns_tuple(self):
        """
        Given a mocked pool returning empty results
        When get_audit_logs() is called
        Then it returns a tuple of (list, int).
        """
        from code_indexer.server.storage.postgres.groups_backend import (
            GroupsPostgresBackend,
        )

        count_row = {"cnt": 0}
        pool, _, cur = _make_pool()
        cur.fetchone.return_value = count_row
        cur.fetchall.return_value = []
        backend = GroupsPostgresBackend(pool)

        result = backend.get_audit_logs()

        assert isinstance(result, tuple)
        assert len(result) == 2
        logs, total = result
        assert isinstance(logs, list)
        assert isinstance(total, int)
