"""
Unit tests for the AC7 shared `ensure_user_group_membership()` primitive
(Story #1593).

AC7 requires ONE shared, idempotent helper used by all four user-creation
paths (REST admin, MCP admin, Web UI, SSO/OIDC) instead of four
independent implementations (Messi anti-duplication). This file covers
the CORE primitive on GroupAccessManager only -- the per-path wiring
(role-based group resolution for REST/MCP/Web, external-group-mapping
resolution for SSO) is covered by separate tests per call site, since
each site resolves WHICH group differently before calling this shared
"assign + audit, idempotently" mechanic.

TDD: written FIRST, against unmodified production code where
`ensure_user_group_membership` does not exist on GroupAccessManager at
all -- every test below is expected to fail with AttributeError until
the method lands.
"""

import json
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.services.group_access_manager import GroupAccessManager


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def manager(temp_db_path):
    return GroupAccessManager(temp_db_path)


class TestEnsureUserGroupMembershipAssignsNewUser:
    def test_assigns_user_to_given_group(self, manager):
        users_group = manager.get_group_by_name("users")

        result = manager.ensure_user_group_membership(
            "brand-new-user", users_group, assigned_by="admin"
        )

        assert result is True
        membership = manager.get_user_membership("brand-new-user")
        assert membership is not None
        assert membership.group_id == users_group.id
        assert membership.assigned_by == "admin"

    def test_writes_audit_entry_with_given_action_type_and_details(self, manager):
        users_group = manager.get_group_by_name("users")

        manager.ensure_user_group_membership(
            "audited-user",
            users_group,
            assigned_by="admin",
            action_type="user_assign",
            audit_details={"group": "users", "source": "test"},
        )

        logs, total = manager.get_audit_logs(target_type="user")
        matching = [log for log in logs if log["target_id"] == "audited-user"]
        assert len(matching) == 1
        assert matching[0]["action_type"] == "user_assign"
        assert json.loads(matching[0]["details"]) == {
            "group": "users",
            "source": "test",
        }

    def test_default_action_type_is_user_group_assign(self, manager):
        users_group = manager.get_group_by_name("users")

        manager.ensure_user_group_membership(
            "default-action-user", users_group, assigned_by="admin"
        )

        logs, _ = manager.get_audit_logs(target_type="user")
        matching = [log for log in logs if log["target_id"] == "default-action-user"]
        assert matching[0]["action_type"] == "user_group_assign"


class TestEnsureUserGroupMembershipIsIdempotent:
    def test_noop_when_user_already_has_a_membership(self, manager):
        users_group = manager.get_group_by_name("users")
        admins_group = manager.get_group_by_name("admins")
        manager.assign_user_to_group(
            "existing-user", admins_group.id, assigned_by="system:pre-existing"
        )

        result = manager.ensure_user_group_membership(
            "existing-user", users_group, assigned_by="admin"
        )

        assert result is True
        # Must NOT have been moved to the "users" group passed in here --
        # idempotent means leave the existing assignment untouched.
        membership = manager.get_user_membership("existing-user")
        assert membership.group_id == admins_group.id
        assert membership.assigned_by == "system:pre-existing"

    def test_noop_does_not_write_a_new_audit_entry(self, manager):
        users_group = manager.get_group_by_name("users")
        admins_group = manager.get_group_by_name("admins")
        manager.assign_user_to_group(
            "idempotent-audit-user", admins_group.id, assigned_by="system:pre-existing"
        )
        _, before_total = manager.get_audit_logs(target_type="user")

        manager.ensure_user_group_membership(
            "idempotent-audit-user", users_group, assigned_by="admin"
        )

        _, after_total = manager.get_audit_logs(target_type="user")
        assert after_total == before_total
