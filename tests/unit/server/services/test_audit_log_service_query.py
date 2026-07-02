"""
Unit tests for AuditLogService.query() method.

Story #399: Audit Log Consolidation & AuditLogService Extraction
AC1: query() replaces GroupAccessManager.get_audit_logs()
AC5: exclude_target_type parameter filters out auth events from groups UI

TDD: These tests are written BEFORE the implementation exists (RED phase).
"""

import sqlite3

import pytest


@pytest.mark.slow
class TestAuditLogServiceQuery:
    """Tests for AuditLogService.query() method."""

    def _seed(self, service, entries):
        """Helper to seed multiple audit log entries."""
        for entry in entries:
            service.log(**entry)

    def test_query_returns_all_entries_no_filters(self, tmp_path):
        """query() with no filters returns all entries."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "admin",
                    "action_type": "user_group_change",
                    "target_type": "user",
                    "target_id": "u1",
                },
            ],
        )

        logs, total = service.query()

        assert total == 2
        assert len(logs) == 2

    def test_query_returns_empty_on_empty_table(self, tmp_path):
        """query() on empty table returns empty list and total=0."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        logs, total = service.query()

        assert total == 0
        assert logs == []

    def test_query_filters_by_action_type(self, tmp_path):
        """query(action_type=X) returns only matching entries."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "admin",
                    "action_type": "user_group_change",
                    "target_type": "user",
                    "target_id": "u1",
                },
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g2",
                },
            ],
        )

        logs, total = service.query(action_type="group_create")

        assert total == 2
        assert all(log["action_type"] == "group_create" for log in logs)

    def test_query_filters_by_target_type(self, tmp_path):
        """query(target_type=X) returns only matching entries."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "admin",
                    "action_type": "user_group_change",
                    "target_type": "user",
                    "target_id": "u1",
                },
                {
                    "admin_id": "sys",
                    "action_type": "authentication_failure",
                    "target_type": "auth",
                    "target_id": "u2",
                },
            ],
        )

        logs, total = service.query(target_type="user")

        assert total == 1
        assert logs[0]["target_type"] == "user"

    def test_query_filters_by_admin_id(self, tmp_path):
        """query(admin_id=X) returns only entries for that admin."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin1",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "admin2",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g2",
                },
                {
                    "admin_id": "admin1",
                    "action_type": "user_group_change",
                    "target_type": "user",
                    "target_id": "u1",
                },
            ],
        )

        logs, total = service.query(admin_id="admin1")

        assert total == 2
        assert all(log["admin_id"] == "admin1" for log in logs)

    def test_query_returns_results_in_descending_timestamp_order(self, tmp_path):
        """query() returns results newest-first."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
            ("2026-01-01T10:00:00", "admin", "first", "group", "g1"),
        )
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
            ("2026-01-02T10:00:00", "admin", "second", "group", "g2"),
        )
        conn.commit()
        conn.close()

        logs, total = service.query()

        assert total == 2
        assert logs[0]["action_type"] == "second"
        assert logs[1]["action_type"] == "first"

    def test_query_respects_limit(self, tmp_path):
        """query(limit=N) returns at most N entries but total reflects full count."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        for i in range(10):
            service.log("admin", "group_create", "group", f"g{i}")

        logs, total = service.query(limit=3)

        assert total == 10  # total count is always the full count
        assert len(logs) == 3

    def test_query_respects_offset(self, tmp_path):
        """query(offset=N) skips N entries (pages don't overlap)."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        for i in range(5):
            service.log("admin", "group_create", "group", f"g{i}")

        logs_page1, _ = service.query(limit=3, offset=0)
        logs_page2, _ = service.query(limit=3, offset=3)

        ids_page1 = {log["id"] for log in logs_page1}
        ids_page2 = {log["id"] for log in logs_page2}
        assert ids_page1.isdisjoint(ids_page2)

    def test_query_returns_dict_with_all_fields(self, tmp_path):
        """query() returns dicts with all expected fields."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        service.log("admin", "group_create", "group", "g1", '{"name": "test"}')

        logs, _ = service.query()

        assert len(logs) == 1
        log = logs[0]
        for field in (
            "id",
            "timestamp",
            "admin_id",
            "action_type",
            "target_type",
            "target_id",
            "details",
        ):
            assert field in log

    def test_query_filters_by_date_from(self, tmp_path):
        """query(date_from='YYYY-MM-DD') filters entries from that date onward."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
            ("2026-01-01T10:00:00", "admin", "old", "group", "g1"),
        )
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
            ("2026-03-01T10:00:00", "admin", "new", "group", "g2"),
        )
        conn.commit()
        conn.close()

        logs, total = service.query(date_from="2026-02-01")

        assert total == 1
        assert logs[0]["action_type"] == "new"

    def test_query_filters_by_date_to(self, tmp_path):
        """query(date_to='YYYY-MM-DD') filters entries up to that date."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
            ("2026-01-01T10:00:00", "admin", "old", "group", "g1"),
        )
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id) VALUES (?, ?, ?, ?, ?)",
            ("2026-03-01T10:00:00", "admin", "new", "group", "g2"),
        )
        conn.commit()
        conn.close()

        logs, total = service.query(date_to="2026-02-01")

        assert total == 1
        assert logs[0]["action_type"] == "old"


@pytest.mark.slow
class TestAuditLogServiceQueryExcludeTargetType:
    """Tests for exclude_target_type parameter (AC5: Groups UI excludes auth events)."""

    def _seed(self, service, entries):
        for entry in entries:
            service.log(**entry)

    def test_exclude_target_type_filters_out_auth_events(self, tmp_path):
        """query(exclude_target_type='auth') excludes auth events (AC5)."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "sys",
                    "action_type": "authentication_failure",
                    "target_type": "auth",
                    "target_id": "u1",
                },
                {
                    "admin_id": "admin",
                    "action_type": "user_group_change",
                    "target_type": "user",
                    "target_id": "u2",
                },
                {
                    "admin_id": "sys",
                    "action_type": "token_refresh_success",
                    "target_type": "auth",
                    "target_id": "u3",
                },
            ],
        )

        logs, total = service.query(exclude_target_type="auth")

        assert total == 2
        assert all(log["target_type"] != "auth" for log in logs)

    def test_exclude_target_type_combined_with_action_type_filter(self, tmp_path):
        """query(exclude_target_type='auth', action_type='group_create') combines filters."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "admin",
                    "action_type": "user_group_change",
                    "target_type": "user",
                    "target_id": "u1",
                },
                {
                    "admin_id": "sys",
                    "action_type": "authentication_failure",
                    "target_type": "auth",
                    "target_id": "u2",
                },
            ],
        )

        logs, total = service.query(
            action_type="group_create", exclude_target_type="auth"
        )

        assert total == 1
        assert logs[0]["action_type"] == "group_create"

    def test_exclude_target_type_none_returns_all(self, tmp_path):
        """query(exclude_target_type=None) returns all entries including auth."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "sys",
                    "action_type": "authentication_failure",
                    "target_type": "auth",
                    "target_id": "u1",
                },
            ],
        )

        logs, total = service.query(exclude_target_type=None)

        assert total == 2

    def test_exclude_target_type_total_reflects_filtered_count(self, tmp_path):
        """total count reflects filtered (excluded) count, not raw table count."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        self._seed(
            service,
            [
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g1",
                },
                {
                    "admin_id": "admin",
                    "action_type": "group_create",
                    "target_type": "group",
                    "target_id": "g2",
                },
                {
                    "admin_id": "sys",
                    "action_type": "auth_event",
                    "target_type": "auth",
                    "target_id": "u1",
                },
                {
                    "admin_id": "sys",
                    "action_type": "auth_event",
                    "target_type": "auth",
                    "target_id": "u2",
                },
                {
                    "admin_id": "sys",
                    "action_type": "auth_event",
                    "target_type": "auth",
                    "target_id": "u3",
                },
            ],
        )

        logs, total = service.query(exclude_target_type="auth")

        assert total == 2  # Only non-auth events count
        assert len(logs) == 2

    def test_exclude_target_type_with_limit_and_offset(self, tmp_path):
        """exclude_target_type works correctly with limit and offset."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        for i in range(6):
            service.log("admin", "group_create", "group", f"g{i}")
        for i in range(4):
            service.log("sys", "auth_event", "auth", f"u{i}")

        logs, total = service.query(exclude_target_type="auth", limit=3, offset=0)

        assert total == 6
        assert len(logs) == 3
        assert all(log["target_type"] == "group" for log in logs)
