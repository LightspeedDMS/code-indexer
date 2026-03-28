"""
Unit tests for AuditLogService.get_pr_logs() and get_cleanup_logs().

Story #399: Audit Log Consolidation & AuditLogService Extraction
AC3: Query methods rewritten for SQLite

Tests verify:
- get_pr_logs() returns entries with PR action_types
- get_pr_logs() supports repo_alias filtering and pagination
- get_cleanup_logs() returns entries with git_cleanup action_type
- get_cleanup_logs() supports repo_path filtering and pagination
- Results are returned in reverse chronological order

TDD: These tests are written BEFORE the implementation exists (RED phase).
"""

import sqlite3


class TestGetPrLogs:
    """Tests for AuditLogService.get_pr_logs() (AC3)."""

    def test_get_pr_logs_returns_pr_action_types(self, tmp_path):
        """get_pr_logs() returns entries with PR-related action_types only."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log(
            "system",
            "pr_creation_success",
            "auth",
            "my-repo",
            '{"job_id": "j1", "pr_url": "https://gh.com/1", "repo_alias": "my-repo"}',
        )
        service.log(
            "system",
            "pr_creation_failure",
            "auth",
            "my-repo",
            '{"job_id": "j2", "repo_alias": "my-repo"}',
        )
        service.log(
            "system",
            "pr_creation_disabled",
            "auth",
            "other-repo",
            '{"job_id": "j3", "repo_alias": "other-repo"}',
        )
        service.log("admin", "group_create", "group", "g1")  # Not a PR event

        logs = service.get_pr_logs()

        assert len(logs) == 3
        action_types = {log["action_type"] for log in logs}
        assert action_types == {
            "pr_creation_success",
            "pr_creation_failure",
            "pr_creation_disabled",
        }

    def test_get_pr_logs_excludes_non_pr_events(self, tmp_path):
        """get_pr_logs() does not include non-PR events."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log("admin", "group_create", "group", "g1")
        service.log(
            "system",
            "git_cleanup",
            "auth",
            "/repo",
            '{"event_type": "git_cleanup", "repo_path": "/repo", "files_cleared": []}',
        )
        service.log("system", "authentication_failure", "auth", "user1")

        logs = service.get_pr_logs()

        assert len(logs) == 0

    def test_get_pr_logs_filters_by_repo_alias(self, tmp_path):
        """get_pr_logs(repo_alias=X) returns only entries for that repo."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log(
            "system",
            "pr_creation_success",
            "auth",
            "repo-a",
            '{"job_id": "j1", "repo_alias": "repo-a"}',
        )
        service.log(
            "system",
            "pr_creation_success",
            "auth",
            "repo-b",
            '{"job_id": "j2", "repo_alias": "repo-b"}',
        )

        logs = service.get_pr_logs(repo_alias="repo-a")

        assert len(logs) == 1
        assert logs[0]["target_id"] == "repo-a"

    def test_get_pr_logs_repo_alias_no_match_returns_empty(self, tmp_path):
        """get_pr_logs(repo_alias='nonexistent') returns empty list."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)
        service.log(
            "system",
            "pr_creation_success",
            "auth",
            "repo-a",
            '{"job_id": "j1", "repo_alias": "repo-a"}',
        )

        logs = service.get_pr_logs(repo_alias="nonexistent")

        assert len(logs) == 0

    def test_get_pr_logs_respects_limit(self, tmp_path):
        """get_pr_logs(limit=N) returns at most N entries."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        for i in range(10):
            service.log(
                "system",
                "pr_creation_success",
                "auth",
                f"repo-{i}",
                f'{{"job_id": "j{i}", "repo_alias": "repo-{i}"}}',
            )

        logs = service.get_pr_logs(limit=3)

        assert len(logs) == 3

    def test_get_pr_logs_respects_offset(self, tmp_path):
        """get_pr_logs(offset=N) skips N entries."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        for i in range(5):
            service.log(
                "system",
                "pr_creation_success",
                "auth",
                f"repo-{i}",
                f'{{"job_id": "j{i}", "repo_alias": "repo-{i}"}}',
            )

        logs_p1 = service.get_pr_logs(limit=3, offset=0)
        logs_p2 = service.get_pr_logs(limit=3, offset=3)

        ids_p1 = {log["id"] for log in logs_p1}
        ids_p2 = {log["id"] for log in logs_p2}
        assert ids_p1.isdisjoint(ids_p2)

    def test_get_pr_logs_returns_newest_first(self, tmp_path):
        """get_pr_logs() returns entries in reverse chronological order."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "2026-01-01T10:00:00",
                "system",
                "pr_creation_success",
                "auth",
                "r1",
                '{"event_type": "pr_creation_success", "repo_alias": "r1"}',
            ),
        )
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "2026-03-01T10:00:00",
                "system",
                "pr_creation_success",
                "auth",
                "r2",
                '{"event_type": "pr_creation_success", "repo_alias": "r2"}',
            ),
        )
        conn.commit()
        conn.close()

        logs = service.get_pr_logs()

        assert len(logs) == 2
        assert logs[0]["target_id"] == "r2"  # Newer first
        assert logs[1]["target_id"] == "r1"

    def test_get_pr_logs_empty_table(self, tmp_path):
        """get_pr_logs() returns empty list when no PR events exist."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        logs = service.get_pr_logs()

        assert logs == []


class TestGetCleanupLogs:
    """Tests for AuditLogService.get_cleanup_logs() (AC3)."""

    def test_get_cleanup_logs_returns_git_cleanup_entries(self, tmp_path):
        """get_cleanup_logs() returns only git_cleanup action_type entries."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log(
            "system",
            "git_cleanup",
            "auth",
            "/path/to/repo",
            '{"event_type": "git_cleanup", "repo_path": "/path/to/repo", "files_cleared": []}',
        )
        service.log("admin", "group_create", "group", "g1")  # Not cleanup
        service.log(
            "system",
            "pr_creation_success",
            "auth",
            "r1",
            '{"event_type": "pr_creation_success"}',
        )  # Not cleanup

        logs = service.get_cleanup_logs()

        assert len(logs) == 1
        assert logs[0]["action_type"] == "git_cleanup"

    def test_get_cleanup_logs_excludes_non_cleanup_events(self, tmp_path):
        """get_cleanup_logs() does not include non-cleanup events."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log("system", "pr_creation_success", "auth", "r1")
        service.log("admin", "group_create", "group", "g1")

        logs = service.get_cleanup_logs()

        assert len(logs) == 0

    def test_get_cleanup_logs_filters_by_repo_path(self, tmp_path):
        """get_cleanup_logs(repo_path=X) returns only entries for that path."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        service.log(
            "system",
            "git_cleanup",
            "auth",
            "/repo-a",
            '{"event_type": "git_cleanup", "repo_path": "/repo-a", "files_cleared": []}',
        )
        service.log(
            "system",
            "git_cleanup",
            "auth",
            "/repo-b",
            '{"event_type": "git_cleanup", "repo_path": "/repo-b", "files_cleared": []}',
        )

        logs = service.get_cleanup_logs(repo_path="/repo-a")

        assert len(logs) == 1
        assert logs[0]["target_id"] == "/repo-a"

    def test_get_cleanup_logs_respects_limit(self, tmp_path):
        """get_cleanup_logs(limit=N) returns at most N entries."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        for i in range(10):
            service.log(
                "system",
                "git_cleanup",
                "auth",
                f"/repo-{i}",
                f'{{"event_type": "git_cleanup", "repo_path": "/repo-{i}", "files_cleared": []}}',
            )

        logs = service.get_cleanup_logs(limit=4)

        assert len(logs) == 4

    def test_get_cleanup_logs_returns_newest_first(self, tmp_path):
        """get_cleanup_logs() returns entries in reverse chronological order."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "2026-01-01T10:00:00",
                "system",
                "git_cleanup",
                "auth",
                "/old-repo",
                '{"event_type": "git_cleanup", "repo_path": "/old-repo", "files_cleared": []}',
            ),
        )
        cursor.execute(
            "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "2026-03-01T10:00:00",
                "system",
                "git_cleanup",
                "auth",
                "/new-repo",
                '{"event_type": "git_cleanup", "repo_path": "/new-repo", "files_cleared": []}',
            ),
        )
        conn.commit()
        conn.close()

        logs = service.get_cleanup_logs()

        assert len(logs) == 2
        assert logs[0]["target_id"] == "/new-repo"  # Newer first
        assert logs[1]["target_id"] == "/old-repo"

    def test_get_cleanup_logs_empty_table(self, tmp_path):
        """get_cleanup_logs() returns empty list when no cleanup events exist."""
        from code_indexer.server.services.audit_log_service import AuditLogService

        db_path = tmp_path / "groups.db"
        service = AuditLogService(db_path)

        logs = service.get_cleanup_logs()

        assert logs == []
