"""
Unit tests for service-layer storage Protocol interfaces (Story #410).

Verifies GroupsBackend (GroupAccessManager) and AuditLogBackend (AuditLogService)
Protocols.

Tests use runtime_checkable isinstance() checks.
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> str:
    """Create and initialise a test SQLite database, return path string."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "test.db")
    DatabaseSchema(db_path).initialize_database()
    return db_path


# ---------------------------------------------------------------------------
# GroupsBackend (GroupAccessManager)
# ---------------------------------------------------------------------------


class TestGroupsBackend:
    def test_group_access_manager_satisfies_protocol(self, tmp_path: Path) -> None:
        """GroupAccessManager must satisfy GroupsBackend protocol."""
        from code_indexer.server.storage.protocols import GroupsBackend
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        db_path = _make_db(tmp_path)
        manager = GroupAccessManager(db_path)

        assert isinstance(manager, GroupsBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy GroupsBackend."""
        from code_indexer.server.storage.protocols import GroupsBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), GroupsBackend)

    def test_protocol_has_required_methods(self) -> None:
        """GroupsBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import GroupsBackend

        required = {
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
        }
        protocol_attrs = set(m for m in dir(GroupsBackend) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)


# ---------------------------------------------------------------------------
# AuditLogBackend (AuditLogService)
# ---------------------------------------------------------------------------


class TestAuditLogBackend:
    def test_audit_log_service_satisfies_protocol(self, tmp_path: Path) -> None:
        """AuditLogService must satisfy AuditLogBackend protocol."""
        from code_indexer.server.storage.protocols import AuditLogBackend
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = _make_db(tmp_path)
        service = AuditLogService(Path(db_path))

        assert isinstance(service, AuditLogBackend)

    def test_dummy_class_does_not_satisfy_protocol(self) -> None:
        """A class missing protocol methods must not satisfy AuditLogBackend."""
        from code_indexer.server.storage.protocols import AuditLogBackend

        class Dummy:
            pass

        assert not isinstance(Dummy(), AuditLogBackend)

    def test_protocol_has_required_methods(self) -> None:
        """AuditLogBackend must declare all required public methods."""
        from code_indexer.server.storage.protocols import AuditLogBackend

        required = {
            "log",
            "log_raw",
            "query",
            "get_pr_logs",
            "get_cleanup_logs",
        }
        protocol_attrs = set(m for m in dir(AuditLogBackend) if not m.startswith("_"))
        assert required.issubset(protocol_attrs)
