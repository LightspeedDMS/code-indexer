"""MCP ``query_audit_logs`` reads through the one shared audit read function.

Real ``AuditLogService`` stores (SQLite, and PostgreSQL when
``TEST_POSTGRES_DSN`` is set) wired onto ``app.state``; rows are written
through the store's single write function.  The handler is called past its
elevation decorator (the gate itself is covered by the elevation tests).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.server.services.audit_log_query import encode_cursor
from tests.unit.server._audit_read_support import build_store, make_event, seed


@pytest.fixture
def admin_user():
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture(params=("sqlite", "postgres"))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    import code_indexer.server.app as app_module

    for built in build_store(request.param, tmp_path):
        sentinel = object()
        previous = getattr(app_module.app.state, "audit_service", sentinel)
        app_module.app.state.audit_service = built
        try:
            yield built
        finally:
            if previous is sentinel:
                del app_module.app.state.audit_service
            else:
                app_module.app.state.audit_service = previous


def _call(user, **args) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

    result = handle_query_audit_logs.__wrapped__(args, user)
    payload: Dict[str, Any] = json.loads(result["content"][0]["text"])
    return payload


def _ok(user, **args) -> Dict[str, Any]:
    payload = _call(user, **args)
    assert payload["success"] is True, payload
    return payload


def _targets(payload: Dict[str, Any]) -> List[str]:
    return [entry["target_id"] for entry in payload["entries"]]


def _ts(minute: int) -> str:
    return f"2026-09-01T10:{minute:02d}:00+00:00"


def _seed(store) -> None:
    seed(
        store,
        [
            make_event(ts=_ts(1), target_id="u1", source="rest"),
            make_event(ts=_ts(2), target_id="u2", ip_address="198.51.100.7"),
            make_event(ts=_ts(3), target_id="u3", correlation_id="corr-3"),
            make_event(ts=_ts(3), target_id="u4", outcome="failure"),
            make_event(ts=_ts(3), target_id="u5"),
            make_event(
                ts=_ts(4),
                action_type="token_refresh_success",
                target_type="auth",
                target_id="u6",
            ),
            make_event(
                ts=_ts(5),
                action_type="security_incident",
                target_type="auth",
                target_id="u7",
            ),
            make_event(
                ts=_ts(6),
                action_type="golden_repo_removed",
                target_type="repo",
                target_id="example-repo",
                details_json=json.dumps({"job_id": "j-1", "note": "free text"}),
            ),
        ],
    )


class TestFiltersAndTiers:
    @pytest.mark.parametrize(
        "args, expected",
        [
            ({"target_type": "repo"}, ["example-repo"]),
            ({"target_id": "u2"}, ["u2"]),
            ({"outcome": "failure"}, ["u4"]),
            ({"source": "rest"}, ["u1"]),
            ({"ip_address": "198.51.100.7"}, ["u2"]),
            ({"correlation_id": "corr-3"}, ["u3"]),
        ],
    )
    def test_new_filters_narrow_the_rows(self, admin_user, store, args, expected):
        _seed(store)
        assert _targets(_ok(admin_user, **args)) == expected

    def test_tier_defaults_to_all_and_security_hides_auth_activity(
        self, admin_user, store
    ):
        _seed(store)
        everything = _targets(_ok(admin_user))
        assert "u6" in everything and len(everything) == 8
        security = _targets(_ok(admin_user, tier="security"))
        assert "u6" not in security and "u7" in security
        assert _targets(_ok(admin_user, tier="auth_activity")) == ["u6"]


class TestCursorPaging:
    def test_walk_older_then_newer_without_gaps_or_duplicates(self, admin_user, store):
        _seed(store)
        expected = _targets(_ok(admin_user, limit=100))
        first = _ok(admin_user, limit=3)
        assert first["total"] == 8 and first["total_capped"] is False
        assert first["has_more"] is True and first["next_cursor"]
        walked, page = _targets(first), first
        for _ in range(5):
            if not page["next_cursor"]:
                break
            page = _ok(admin_user, limit=3, cursor=page["next_cursor"])
            walked += _targets(page)
        assert walked == expected
        assert page["has_more"] is False and page["next_cursor"] is None
        start = len(walked) - len(_targets(page))
        back = _ok(admin_user, limit=3, cursor=page["prev_cursor"], direction="newer")
        assert _targets(back) == expected[start - 3 : start]

    def test_page_argument_still_pages_by_offset(self, admin_user, store):
        _seed(store)
        second = _ok(admin_user, limit=3, page=2)
        assert _targets(second) == _targets(_ok(admin_user, limit=100))[3:6]


class TestEntries:
    def test_entry_carries_every_shared_field_and_decoded_allowlisted_details(
        self, admin_user, store
    ):
        from code_indexer.server.services.audit_log_query import AUDIT_ROW_FIELDS

        _seed(store)
        entry = _ok(admin_user, target_type="repo")["entries"][0]
        assert set(AUDIT_ROW_FIELDS) <= set(entry)
        assert entry["details"] == {"job_id": "j-1", "omitted_fields": ["note"]}
        assert entry["submitted_only"] is True
        assert entry["pairing_state"] is None
        assert entry["actor_is_authenticated"] is True
        assert entry["resource"] == entry["target_id"] == "example-repo"
        assert entry["user"] == entry["admin_id"]


_PR_URL = "https://forge.example.com/example-org/example-repo/pull/7"


def _pr_row(store, pr_url: Any) -> None:
    """One PR-creation row as the PR audit writer records it."""
    seed(
        store,
        [
            make_event(
                ts=_ts(9),
                action_type="pr_creation_success",
                target_type="auth",
                target_id="example-repo",
                actor="system",
                details_json=json.dumps(
                    {
                        "job_id": "job-7",
                        "repo_alias": "example-repo",
                        "branch_name": "fix/example",
                        "pr_url": pr_url,
                        "commit_hash": "abc123",
                        "files_modified": ["src/example.py"],
                    }
                ),
            )
        ],
    )


class TestPrCreationResource:
    """PR-creation entries keep ``resource`` = the recorded PR URL, read
    through the details allowlist (never around it)."""

    def test_resource_is_the_recorded_pr_url(self, admin_user, store):
        _pr_row(store, _PR_URL)
        (entry,) = _ok(admin_user, action_type="pr_creation_success")["entries"]
        assert entry["resource"] == _PR_URL
        assert entry["details"]["pr_url"] == _PR_URL
        assert entry["target_id"] == "example-repo"
        assert "files_modified" in entry["details"]["omitted_fields"]

    def test_userinfo_query_and_fragment_are_stripped(self, admin_user, store):
        _pr_row(
            store,
            "https://ci-bot:example-secret@forge.example.com/example-org/"
            "example-repo/pull/7?private_token=example-secret#top",
        )
        payload = _ok(admin_user, action_type="pr_creation_success")
        (entry,) = payload["entries"]
        assert entry["resource"] == _PR_URL
        assert entry["details"]["pr_url"] == _PR_URL
        assert "example-secret" not in json.dumps(payload)

    @pytest.mark.parametrize(
        "stored",
        ["javascript:alert(1)", "not a url", "ftp://forge.example.com/x", 7, None],
    )
    def test_a_value_that_is_not_a_web_url_is_omitted(self, admin_user, store, stored):
        _pr_row(store, stored)
        (entry,) = _ok(admin_user, action_type="pr_creation_success")["entries"]
        assert "pr_url" not in entry["details"]
        assert "pr_url" in entry["details"]["omitted_fields"]
        assert entry["resource"] == entry["target_id"] == "example-repo"


class TestAggregate:
    def test_aggregate_groups_authentication_activity(self, admin_user, store):
        _seed(store)
        payload = _ok(admin_user, tier="auth_activity", aggregate=True, all_time=True)
        assert payload["entries"] == []
        assert [(g["action_type"], g["count"]) for g in payload["groups"]] == [
            ("token_refresh_success", 1)
        ]
        assert payload["all_time"] is True and payload["truncated"] is False


class TestToolDoc:
    def test_schema_declares_every_argument_with_the_code_enums(self):
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        from code_indexer.server.services.audit_events import OUTCOMES
        from code_indexer.server.services.audit_log_query import (
            AUDIT_DIRECTIONS,
            AUDIT_LOG_MAX_LIMIT,
            AUDIT_SOURCES,
            AUDIT_TIERS,
            DEFAULT_AUDIT_LOG_LIMIT,
        )

        props = TOOL_REGISTRY["query_audit_logs"]["inputSchema"]["properties"]
        for name in (
            "user",
            "action",
            "action_type",
            "target_type",
            "target_id",
            "outcome",
            "source",
            "ip_address",
            "correlation_id",
            "from_date",
            "to_date",
            "tier",
            "cursor",
            "direction",
            "limit",
            "page",
            "aggregate",
            "all_time",
        ):
            assert name in props, name
        assert set(props["tier"]["enum"]) == AUDIT_TIERS
        assert props["tier"]["default"] == "all"
        assert set(props["direction"]["enum"]) == AUDIT_DIRECTIONS
        assert set(props["outcome"]["enum"]) == OUTCOMES
        assert set(props["source"]["enum"]) == AUDIT_SOURCES
        assert props["aggregate"]["type"] == props["all_time"]["type"] == "boolean"
        assert props["limit"]["default"] == DEFAULT_AUDIT_LOG_LIMIT
        assert props["limit"]["maximum"] == AUDIT_LOG_MAX_LIMIT


class TestRefusals:
    @pytest.mark.parametrize("page", [1, 2])
    def test_an_explicit_page_with_a_cursor_is_refused(self, admin_user, store, page):
        _seed(store)
        cursor = _ok(admin_user, limit=2)["next_cursor"]
        payload = _call(admin_user, cursor=cursor, page=page)
        assert payload["success"] is False
        assert payload["error"] == "cursor and page cannot be combined"

    @pytest.mark.parametrize(
        "args",
        [
            {"cursor": "not-a-cursor"},
            {"cursor": encode_cursor("2026-09-01T10:03:00+00:00", 1), "page": 2},
            {"direction": "newer"},
            {"tier": "everything"},
            {"outcome": "maybe"},
            {"from_date": "yesterday"},
            {"aggregate": "yes", "tier": "auth_activity"},
            {"aggregate": True, "tier": "security"},
        ],
    )
    def test_bad_arguments_return_success_false(self, admin_user, store, args):
        payload = _call(admin_user, **args)
        assert payload["success"] is False
        assert payload["error"]
        assert "entries" not in payload

    @pytest.mark.parametrize(
        "args",
        [{"tier": ["all"]}, {"direction": 5}, {"cursor": 5}, {"tier": {"a": 1}}],
    )
    def test_wrong_type_arguments_are_refused_without_an_error_log(
        self, admin_user, store, args, caplog
    ):
        """A wrong-typed argument is a bad argument, not a server error."""
        import logging

        with caplog.at_level(logging.WARNING):
            payload = _call(admin_user, **args)
        assert payload["success"] is False and payload["error"]
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
