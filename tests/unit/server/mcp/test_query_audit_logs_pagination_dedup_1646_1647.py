"""Regression tests for GitHub issues #1646 and #1647.

Both bugs live in the SAME function: handle_query_audit_logs
(src/code_indexer/server/mcp/handlers/admin/__init__.py).

Issue #1646: handle_query_audit_logs ignores the `page` argument entirely --
every call returns the same first-`limit` slice regardless of `page`, and the
`"total"` field in the response reports the size of the *returned page*
(len(filtered)) rather than the true number of matching rows.

Issue #1647: the handler merges get_pr_logs()/get_cleanup_logs() results with
a general audit_service.query() call that re-selects the SAME underlying
audit_logs rows (PR/cleanup events are stored with target_type="auth" just
like every other event, so an unfiltered general query returns them again),
producing duplicate entries for pr_creation_*/git_cleanup events.

These tests use a REAL AuditLogService backed by a real temporary SQLite
database (no mocks) -- matching the project's anti-mock convention and the
sibling test file test_query_audit_logs_datetime_serialization_1642.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from code_indexer.server.services.audit_log_service import AuditLogService


@pytest.fixture
def admin_user():
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def audit_service(tmp_path):
    """Real AuditLogService backed by a real temp SQLite file."""
    db_path = tmp_path / "audit_1646_1647.db"
    return AuditLogService(db_path)


@pytest.fixture
def wired_audit_service(audit_service):
    """Install the real AuditLogService onto app.state for the test duration."""
    import code_indexer.server.app as app_module

    sentinel = object()
    previous = getattr(app_module.app.state, "audit_service", sentinel)
    app_module.app.state.audit_service = audit_service
    try:
        yield audit_service
    finally:
        if previous is sentinel:
            del app_module.app.state.audit_service
        else:
            app_module.app.state.audit_service = previous


def _insert_general_row(service: AuditLogService, *, ts: str, target_id: str) -> None:
    """Insert one ordinary (non-PR/non-cleanup) audit row with an explicit
    timestamp so ordering across rows is deterministic."""
    service.log_raw(
        timestamp=ts,
        admin_id="admin",
        action_type="user_created",
        target_type="user",
        target_id=target_id,
        details="{}",
    )


def _insert_pr_creation_row(
    service: AuditLogService, *, ts: str, repo_alias: str
) -> None:
    """Insert one pr_creation_success row, matching the real
    PasswordChangeAuditLogger._log_to_service field mapping: target_type is
    ALWAYS "auth" for these events (see audit_logger.py docstring)."""
    details = json.dumps(
        {
            "event_type": "pr_creation_success",
            "repo_alias": repo_alias,
            "pr_url": f"https://example.invalid/{repo_alias}/pull/1",
            "timestamp": ts,
        }
    )
    service.log_raw(
        timestamp=ts,
        admin_id="system",
        action_type="pr_creation_success",
        target_type="auth",
        target_id=repo_alias,
        details=details,
    )


def _insert_git_cleanup_row(
    service: AuditLogService, *, ts: str, repo_path: str
) -> None:
    """Insert one git_cleanup row -- also target_type="auth" per the real
    _log_to_service mapping."""
    details = json.dumps(
        {
            "event_type": "git_cleanup",
            "repo_path": repo_path,
            "timestamp": ts,
        }
    )
    service.log_raw(
        timestamp=ts,
        admin_id="system",
        action_type="git_cleanup",
        target_type="auth",
        target_id=repo_path,
        details=details,
    )


class TestQueryAuditLogsPaginationBug1646:
    """page argument is currently ignored; total reports page-size not true count."""

    def test_page_2_returns_different_entry_than_page_1(
        self, admin_user, wired_audit_service
    ):
        """Reproduces #1646: with limit=1, page=2 must return a DIFFERENT
        entry than page=1 (the second-newest row), not an identical repeat.
        """
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        _insert_general_row(
            wired_audit_service, ts="2026-08-20T10:00:00+00:00", target_id="alice"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-21T10:00:00+00:00", target_id="bob"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-22T10:00:00+00:00", target_id="carol"
        )

        page1 = handle_query_audit_logs.__wrapped__({"limit": 1, "page": 1}, admin_user)
        page2 = handle_query_audit_logs.__wrapped__({"limit": 1, "page": 2}, admin_user)

        payload1 = json.loads(page1["content"][0]["text"])
        payload2 = json.loads(page2["content"][0]["text"])

        assert payload1["success"] is True
        assert payload2["success"] is True
        assert len(payload1["entries"]) == 1
        assert len(payload2["entries"]) == 1

        # Newest-first ordering: page 1 -> carol (2026-08-22), page 2 -> bob (2026-08-21).
        assert payload1["entries"][0]["resource"] == "carol"
        assert payload2["entries"][0]["resource"] == "bob", (
            "page=2 must return the SECOND newest row, not a repeat of page=1 "
            "-- this is the exact non-functional-pagination symptom in #1646"
        )

    def test_total_reflects_true_row_count_not_page_size(
        self, admin_user, wired_audit_service
    ):
        """Reproduces #1646: total must reflect the true number of matching
        rows (3), not just the size of the returned page (1)."""
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        _insert_general_row(
            wired_audit_service, ts="2026-08-20T10:00:00+00:00", target_id="alice"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-21T10:00:00+00:00", target_id="bob"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-22T10:00:00+00:00", target_id="carol"
        )

        result = handle_query_audit_logs.__wrapped__(
            {"limit": 1, "page": 1}, admin_user
        )
        payload = json.loads(result["content"][0]["text"])

        assert payload["success"] is True
        assert len(payload["entries"]) == 1
        assert payload["total"] == 3, (
            "total must be the true matching-row count, not len(entries) "
            "-- this is the second symptom in #1646"
        )


class TestQueryAuditLogsDedupBug1647:
    """get_pr_logs()/get_cleanup_logs() rows are re-selected by the general
    audit_service.query() call, duplicating them in the merged response."""

    def test_pr_creation_event_not_duplicated(self, admin_user, wired_audit_service):
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        _insert_pr_creation_row(
            wired_audit_service, ts="2026-08-20T10:00:00+00:00", repo_alias="my-repo"
        )

        result = handle_query_audit_logs.__wrapped__({}, admin_user)
        payload = json.loads(result["content"][0]["text"])

        assert payload["success"] is True
        matching = [
            e
            for e in payload["entries"]
            if e.get("action_type") == "pr_creation_success"
        ]
        assert len(matching) == 1, (
            f"pr_creation_success event duplicated: found {len(matching)} entries, "
            "expected exactly 1 -- this is the exact symptom in #1647"
        )

    def test_git_cleanup_event_not_duplicated(self, admin_user, wired_audit_service):
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        _insert_git_cleanup_row(
            wired_audit_service,
            ts="2026-08-20T10:00:00+00:00",
            repo_path="/repos/my-repo",
        )

        result = handle_query_audit_logs.__wrapped__({}, admin_user)
        payload = json.loads(result["content"][0]["text"])

        assert payload["success"] is True
        matching = [
            e for e in payload["entries"] if e.get("action_type") == "git_cleanup"
        ]
        assert len(matching) == 1, (
            f"git_cleanup event duplicated: found {len(matching)} entries, "
            "expected exactly 1 -- this is the exact symptom in #1647"
        )


class TestQueryAuditLogsPaginationDedupInteraction:
    """Dedup must happen BEFORE offset/limit slicing, or a page can silently
    lose rows / report a wrong total once duplicates are removed."""

    def test_total_excludes_duplicate_rows_after_dedup(
        self, admin_user, wired_audit_service
    ):
        """3 unique underlying rows (1 pr_creation + 2 general) must produce
        total == 3, never 4 (which is what a naive un-deduped merge, i.e. the
        pr_creation row counted twice, would report)."""
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        _insert_pr_creation_row(
            wired_audit_service, ts="2026-08-20T09:00:00+00:00", repo_alias="my-repo"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-20T10:00:00+00:00", target_id="alice"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-20T11:00:00+00:00", target_id="bob"
        )

        result = handle_query_audit_logs.__wrapped__(
            {"limit": 100, "page": 1}, admin_user
        )
        payload = json.loads(result["content"][0]["text"])

        assert payload["success"] is True
        assert payload["total"] == 3
        assert len(payload["entries"]) == 3

    def test_page_2_still_correct_when_duplicates_are_removed(
        self, admin_user, wired_audit_service
    ):
        """With 3 unique rows and limit=2: page=1 must return 2 entries and
        page=2 must return exactly the 1 remaining entry -- proving the
        offset/limit slice operates on the DEDUPED set, not a pre-dedup
        (inflated) count."""
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        _insert_pr_creation_row(
            wired_audit_service, ts="2026-08-20T09:00:00+00:00", repo_alias="my-repo"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-20T10:00:00+00:00", target_id="alice"
        )
        _insert_general_row(
            wired_audit_service, ts="2026-08-20T11:00:00+00:00", target_id="bob"
        )

        page1 = handle_query_audit_logs.__wrapped__({"limit": 2, "page": 1}, admin_user)
        page2 = handle_query_audit_logs.__wrapped__({"limit": 2, "page": 2}, admin_user)

        payload1 = json.loads(page1["content"][0]["text"])
        payload2 = json.loads(page2["content"][0]["text"])

        assert payload1["success"] is True
        assert payload2["success"] is True
        assert len(payload1["entries"]) == 2
        assert len(payload2["entries"]) == 1
        assert payload1["total"] == 3
        assert payload2["total"] == 3
