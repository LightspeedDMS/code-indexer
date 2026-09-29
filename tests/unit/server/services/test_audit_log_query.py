"""The single shared audit read function (``query_audit_log``).

Runs every scenario against real stores: SQLite, the solo-mode wrapped
SQLite shape, and PostgreSQL (when ``TEST_POSTGRES_DSN`` is set).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.services.audit_log_query import (
    AUDIT_COUNT_CAP,
    AUDIT_LOG_MAX_LIMIT,
    AUDIT_LOG_MAX_OFFSET,
    AUTH_ACTIVITY_DEFAULT_WINDOW,
    DEFAULT_AUDIT_LOG_LIMIT,
    PAIRING_PENDING,
    PAIRING_UNKNOWN,
    SECURITY_PROMOTED_AUTH_ACTIONS,
    AuditAggregate,
    AuditFilters,
    AuditPage,
    AuditQueryError,
    build_filters,
    clamp_limit,
    decode_details,
    encode_cursor,
    plain_web_url,
    query_audit_log,
)
from code_indexer.server.services.audit_log_service import AuditLogService

from tests.unit.server._audit_read_support import (
    STORE_KINDS,
    build_store,
    ids_of,
    make_event,
    seed,
)


@pytest.fixture(params=STORE_KINDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[AuditLogService]:
    yield from build_store(request.param, tmp_path)


def _ts(minute: int, second: int = 0) -> str:
    return f"2026-09-01T10:{minute:02d}:{second:02d}+00:00"


def _seed_shared_timestamps(store: AuditLogService) -> None:
    """Seven rows; three share one timestamp so they straddle page borders."""
    seed(
        store,
        [
            make_event(ts=_ts(1), target_id="u1"),
            make_event(ts=_ts(2), target_id="u2"),
            make_event(ts=_ts(3), target_id="u3"),
            make_event(ts=_ts(3), target_id="u4"),
            make_event(ts=_ts(3), target_id="u5"),
            make_event(ts=_ts(4), target_id="u6"),
            make_event(ts=_ts(5), target_id="u7"),
        ],
    )


def _all_ids_newest_first(store: AuditLogService) -> list:
    page = query_audit_log(store, AuditFilters(), tier="all", limit=1000)
    assert isinstance(page, AuditPage)
    return ids_of(page)


class TestKeysetPaging:
    def test_first_page_orders_by_timestamp_then_id_descending(self, store):
        _seed_shared_timestamps(store)
        page = query_audit_log(store, AuditFilters(), tier="all", limit=1000)
        assert isinstance(page, AuditPage)
        targets = [row.target_id for row in page.rows]
        # Rows sharing 10:03 come out newest id first.
        assert targets == ["u7", "u6", "u5", "u4", "u3", "u2", "u1"]
        assert page.has_more is False
        assert page.next_cursor is None

    def test_walk_older_then_newer_has_no_duplicates_or_gaps(self, store):
        _seed_shared_timestamps(store)
        expected = _all_ids_newest_first(store)

        walked = []
        pages = []
        page = query_audit_log(store, AuditFilters(), tier="all", limit=2)
        assert isinstance(page, AuditPage)
        pages.append(page)
        walked.extend(ids_of(page))
        guard = 0
        while page.next_cursor is not None:
            guard += 1
            assert guard < 10
            page = query_audit_log(
                store,
                AuditFilters(),
                tier="all",
                limit=2,
                cursor=page.next_cursor,
                direction="older",
            )
            assert isinstance(page, AuditPage)
            pages.append(page)
            walked.extend(ids_of(page))
        assert walked == expected
        assert pages[-1].has_more is False

        # Now walk back towards the newest rows from the last page.
        back = list(ids_of(pages[-1]))
        page = pages[-1]
        guard = 0
        while page.has_newer:
            guard += 1
            assert guard < 10
            page = query_audit_log(
                store,
                AuditFilters(),
                tier="all",
                limit=2,
                cursor=page.prev_cursor,
                direction="newer",
            )
            assert isinstance(page, AuditPage)
            back = ids_of(page) + back
        assert back == expected

    def test_newer_page_rows_are_newest_first(self, store):
        _seed_shared_timestamps(store)
        expected = _all_ids_newest_first(store)
        first = query_audit_log(store, AuditFilters(), tier="all", limit=3)
        older = query_audit_log(
            store, AuditFilters(), tier="all", limit=3, cursor=first.next_cursor
        )
        newer = query_audit_log(
            store,
            AuditFilters(),
            tier="all",
            limit=3,
            cursor=older.prev_cursor,
            direction="newer",
        )
        assert ids_of(newer) == expected[:3]
        assert newer.has_newer is False
        assert newer.has_more is False
        assert newer.next_cursor is not None


def _actions(page) -> set:
    return {row.action_type for row in page.rows}


class TestTiers:
    def _seed(self, store):
        rows = [
            make_event(ts=_ts(1), action_type="user_deleted", target_type="user"),
            make_event(
                ts=_ts(2), action_type="token_refresh_success", target_type="auth"
            ),
            make_event(
                ts=_ts(3), action_type="authentication_failure", target_type="auth"
            ),
            make_event(
                ts=_ts(4), action_type="oauth_authorization", target_type="auth"
            ),
        ]
        for i, action in enumerate(SECURITY_PROMOTED_AUTH_ACTIONS):
            rows.append(
                make_event(ts=_ts(10 + i), action_type=action, target_type="auth")
            )
        seed(store, rows)

    def test_security_tier_is_non_auth_rows_plus_every_promoted_auth_action(
        self, store
    ):
        self._seed(store)
        page = query_audit_log(store, AuditFilters(), tier="security", limit=100)
        assert _actions(page) == {"user_deleted", *SECURITY_PROMOTED_AUTH_ACTIONS}
        assert page.total == 1 + len(SECURITY_PROMOTED_AUTH_ACTIONS)

    def test_impersonation_is_promoted_into_the_security_tier(self):
        assert {
            "impersonation_set",
            "impersonation_cleared",
            "impersonation_denied",
        } <= set(SECURITY_PROMOTED_AUTH_ACTIONS)

    def test_auth_activity_tier_is_the_complement_inside_auth(self, store):
        self._seed(store)
        page = query_audit_log(store, AuditFilters(), tier="auth_activity", limit=100)
        assert _actions(page) == {
            "token_refresh_success",
            "authentication_failure",
            "oauth_authorization",
        }
        assert page.total == 3

    def test_all_tier_has_every_row(self, store):
        self._seed(store)
        page = query_audit_log(store, AuditFilters(), tier="all", limit=100)
        assert page.total == 4 + len(SECURITY_PROMOTED_AUTH_ACTIONS)


class TestFilters:
    def _seed(self, store):
        seed(
            store,
            [
                make_event(
                    ts="2026-09-01T00:00:00+00:00",
                    action_type="mcp_credential_created",
                    target_type="mcp_credential",
                    target_id="cred-1",
                    actor="alice",
                    outcome="attempted",
                    source="mcp",
                    ip_address="198.51.100.7",
                    correlation_id="corr-A",
                ),
                make_event(
                    ts="2026-09-01T23:59:59.500000+00:00",
                    action_type="user_deleted",
                    target_id="carol",
                    actor="bob",
                    outcome="failure",
                    source="rest",
                    correlation_id="corr-B",
                ),
                make_event(
                    ts="2026-09-02T00:00:00+00:00",
                    action_type="user_deleted",
                    target_id="dave",
                    actor="alice",
                    source="web",
                    correlation_id="corr-C",
                ),
            ],
        )

    @pytest.mark.parametrize(
        "kwargs, expected_targets",
        [
            ({"action_type": "user_deleted"}, ["dave", "carol"]),
            ({"actor": "alice"}, ["dave", "cred-1"]),
            ({"target_type": "mcp_credential"}, ["cred-1"]),
            ({"target_id": "carol"}, ["carol"]),
            ({"outcome": "failure"}, ["carol"]),
            ({"source": "mcp"}, ["cred-1"]),
            ({"ip_address": "198.51.100.7"}, ["cred-1"]),
            ({"correlation_id": "corr-C"}, ["dave"]),
            ({"date_from": "2026-09-01", "date_to": "2026-09-01"}, ["carol", "cred-1"]),
            ({"date_from": "2026-09-01T23:59:59.500000"}, ["dave", "carol"]),
            ({"date_to": "2026-09-01T00:00:00Z"}, ["cred-1"]),
            ({"actor": "alice", "source": "web"}, ["dave"]),
        ],
    )
    def test_each_filter_narrows_the_rows(self, store, kwargs, expected_targets):
        self._seed(store)
        page = query_audit_log(store, build_filters(**kwargs), tier="all")
        assert [row.target_id for row in page.rows] == expected_targets
        assert page.total == len(expected_targets)

    def test_empty_string_filters_mean_no_filter(self, store):
        self._seed(store)
        page = query_audit_log(
            store, build_filters(actor="", outcome="  ", date_from=""), tier="all"
        )
        assert page.total == 3


class TestCappedCount:
    def test_total_is_exact_up_to_the_cap_and_capped_above(self, store):
        seed(
            store,
            [
                make_event(ts=f"2026-09-01T10:00:{i % 60:02d}+00:00", target_id="u")
                for i in range(AUDIT_COUNT_CAP + 5)
            ],
        )
        page = query_audit_log(store, AuditFilters(), tier="all", limit=10)
        assert page.total == AUDIT_COUNT_CAP
        assert page.total_capped is True
        exact = query_audit_log(store, build_filters(target_id="nobody"), tier="all")
        assert exact.total == 0 and exact.total_capped is False

    def test_limit_is_clamped(self, store):
        seed(store, [make_event(ts=_ts(i % 60)) for i in range(3)])
        assert len(query_audit_log(store, limit=0).rows) == 3
        assert clamp_limit(None) == DEFAULT_AUDIT_LOG_LIMIT
        assert clamp_limit(5000) == AUDIT_LOG_MAX_LIMIT


_NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _ago(**delta) -> str:
    return (_NOW - timedelta(**delta)).isoformat()


class _CallRecorder:
    """Delegates to the real store and records which read methods ran."""

    def __init__(self, store):
        self._store = store
        self.calls: list = []

    def __getattr__(self, name):
        target = getattr(self._store, name)
        self.calls.append(name)
        return target


class TestAggregate:
    def _seed(self, store):
        rows = []
        for i in range(5):  # old: outside the default 24 h window
            rows.append(
                make_event(
                    ts=_ago(days=3, minutes=i),
                    action_type="authentication_failure",
                    target_type="auth",
                    outcome="failure",
                    actor=f"old{i}",
                    auth_method="none",
                )
            )
        for i in range(4):
            rows.append(
                make_event(
                    ts=_ago(hours=1, minutes=i),
                    action_type="authentication_failure",
                    target_type="auth",
                    outcome="failure",
                    actor=f"user{i % 2}",
                    ip_address=f"203.0.113.{i}",
                    auth_method="none",
                )
            )
        rows.append(
            make_event(
                ts=_ago(hours=2),
                action_type="token_refresh_success",
                target_type="auth",
                outcome="success",
            )
        )
        rows.append(  # promoted: never part of authentication activity
            make_event(
                ts=_ago(hours=2), action_type="security_incident", target_type="auth"
            )
        )
        seed(store, rows)

    def test_default_window_is_24h_and_groups_are_sql_aggregates(self, store):
        self._seed(store)
        recorder = _CallRecorder(store)
        result = query_audit_log(
            recorder, tier="auth_activity", aggregate=True, now=_NOW
        )
        assert isinstance(result, AuditAggregate)
        assert recorder.calls == ["aggregate"]  # no raw rows fetched
        groups = {(g.action_type, g.outcome): g for g in result.groups}
        assert set(groups) == {
            ("authentication_failure", "failure"),
            ("token_refresh_success", "success"),
        }
        failures = groups[("authentication_failure", "failure")]
        assert failures.count == 4
        assert failures.distinct_actors == 2
        assert failures.distinct_ips == 4
        assert failures.first_seen == _ago(hours=1, minutes=3)
        assert failures.last_seen == _ago(hours=1)
        assert result.groups[0].action_type == "authentication_failure"
        assert result.window_from == (_NOW - AUTH_ACTIVITY_DEFAULT_WINDOW).isoformat()
        assert result.window_to == _NOW.isoformat()
        assert result.all_time is False

    def test_all_time_lifts_the_window(self, store):
        self._seed(store)
        result = query_audit_log(
            store, tier="auth_activity", aggregate=True, all_time=True, now=_NOW
        )
        counts = {g.action_type: g.count for g in result.groups}
        assert counts["authentication_failure"] == 9
        assert result.window_from is None and result.window_to is None

    def test_explicit_range_replaces_the_default_window(self, store):
        self._seed(store)
        result = query_audit_log(
            store,
            build_filters(date_from=_ago(days=4), date_to=_ago(days=2)),
            tier="auth_activity",
            aggregate=True,
            now=_NOW,
        )
        assert [(g.action_type, g.count) for g in result.groups] == [
            ("authentication_failure", 5)
        ]

    def test_drill_down_lists_the_group_rows(self, store):
        self._seed(store)
        page = query_audit_log(
            store,
            build_filters(
                action_type="authentication_failure",
                outcome="failure",
                date_from=_ago(hours=24),
            ),
            tier="auth_activity",
            limit=2,
            now=_NOW,
        )
        assert page.total == 4 and len(page.rows) == 2 and page.has_more
        assert all(row.actor_is_authenticated is False for row in page.rows)


class TestRowSemantics:
    def _attempted(self, ts, corr, target="cred-1"):
        return make_event(
            ts=ts,
            action_type="mcp_credential_created",
            target_type="mcp_credential",
            target_id=target,
            outcome="attempted",
            correlation_id=corr,
        )

    def test_pairing_state_of_attempted_rows(self, store):
        seed(
            store,
            [
                self._attempted(_ago(seconds=30), "corr-recent"),
                self._attempted(_ago(hours=2), "corr-old"),
                self._attempted(_ago(hours=3), "corr-paired", target="cred-9"),
                make_event(
                    ts=_ago(hours=3),
                    action_type="mcp_credential_created",
                    target_type="mcp_credential",
                    target_id="cred-9",
                    outcome="success",
                    correlation_id="corr-paired",
                ),
            ],
        )
        page = query_audit_log(store, tier="all", now=_NOW)
        states = {
            (row.correlation_id, row.outcome): row.pairing_state for row in page.rows
        }
        assert states[("corr-recent", "attempted")] == PAIRING_PENDING
        assert states[("corr-old", "attempted")] == PAIRING_UNKNOWN
        assert states[("corr-paired", "attempted")] is None
        assert states[("corr-paired", "success")] is None

    def test_row_fields(self, store):
        seed(
            store,
            [
                make_event(
                    ts=_ts(1),
                    action_type="golden_repo_removed",
                    target_type="repo",
                    target_id="example-repo",
                    details_json='{"job_id": "j1"}',
                    actor="system:golden-repo-reconciler",
                    actor_is_system=True,
                    auth_method="system",
                    source="system",
                    ip_address=None,
                    node_id="node-a",
                ),
                make_event(
                    ts=_ts(2),
                    action_type="group_create",
                    target_type="group",
                    target_id="7",
                    outcome=None,
                    source=None,
                    ip_address=None,
                    auth_method=None,
                    details_json="legacy free text",
                ),
            ],
        )
        legacy, job = query_audit_log(store, tier="all").rows
        assert job.submitted_only is True and legacy.submitted_only is False
        assert job.actor_is_system is True and legacy.actor_is_system is False
        assert job.details == '{"job_id": "j1"}'  # allowlisted fields, as JSON
        assert job.node_id == "node-a"
        assert (legacy.outcome, legacy.source, legacy.ip_address) == (None, None, None)
        assert "legacy free text" not in (legacy.details or "")
        assert decode_details(legacy.details) == {"omitted_fields": ["(unstructured)"]}
        assert decode_details(job.details) == {"job_id": "j1"}
        assert decode_details(None) == {}
        assert job.timestamp.endswith("+00:00")


class TestDetailsAllowlist:
    """Only allowlisted ``details`` fields ever leave the read path."""

    def _project(self, action_type, raw):
        from code_indexer.server.services.audit_log_query import project_details

        projected = project_details(action_type, raw)
        return None if projected is None else json.loads(projected)

    def test_catalog_type_keeps_conforming_fields_and_names_the_rest(self):
        raw = json.dumps(
            {
                "job_id": "job-1",
                "repo_host": "https://user:pw@example.com/x",  # not a hostname
                "branch": "main",
                "note": "free text",
            }
        )
        assert self._project("golden_repo_added", raw) == {
            "job_id": "job-1",
            "branch": "main",
            "omitted_fields": ["note", "repo_host"],
        }

    def test_legacy_only_type_uses_its_read_schema(self):
        raw = json.dumps(
            {"name": "ops-team", "description": "<b>free</b>", "source": "mcp"}
        )
        assert self._project("group_create", raw) == {
            "name": "ops-team",
            "omitted_fields": ["description", "source"],
        }

    @pytest.mark.parametrize("raw", ["legacy free text", "[1, 2]", "42", "null"])
    def test_unstructured_content_is_summarised_never_echoed(self, raw):
        assert self._project("group_create", raw) == {
            "omitted_fields": ["(unstructured)"]
        }

    def test_unknown_action_type_keeps_nothing(self):
        raw = json.dumps({"password": "hunter2", "username": "alice"})
        assert self._project("not_in_catalog", raw) == {
            "omitted_fields": ["password", "username"]
        }

    def test_field_names_that_are_not_identifiers_are_not_echoed(self):
        raw = json.dumps({"token=abc123": 1, "name": "ops"})
        assert self._project("group_delete", raw) == {
            "name": "ops",
            "omitted_fields": ["(non-identifier)"],
        }

    @pytest.mark.parametrize("raw", [None, "", "{}"])
    def test_empty_details_stay_empty(self, raw):
        assert self._project("group_create", raw) is None

    def test_the_read_path_applies_the_allowlist_on_every_store(self, store):
        secret = "sk-EXAMPLE-not-a-real-value"
        seed(
            store,
            [
                make_event(
                    ts=_ts(1),
                    action_type="password_change_failure",
                    target_type="auth",
                    target_id="alice",
                    outcome="failure",
                    details_json=json.dumps(
                        {"username": "alice", "user_agent": secret, "reason": secret}
                    ),
                ),
                make_event(
                    ts=_ts(2),
                    action_type="group_create",
                    target_type="group",
                    target_id="8",
                    outcome=None,
                    details_json=f"created with key {secret}",
                ),
            ],
        )
        legacy_text, legacy_json = query_audit_log(store, tier="all").rows
        assert secret not in (legacy_text.details or "")
        assert secret not in (legacy_json.details or "")
        assert json.loads(legacy_json.details or "") == {
            "username": "alice",
            "omitted_fields": ["reason", "user_agent"],
        }
        assert decode_details(legacy_text.details) == {
            "omitted_fields": ["(unstructured)"]
        }


class TestPlainWebUrl:
    """Stored URLs are shown as scheme + host + port + path only."""

    @pytest.mark.parametrize(
        "stored, shown",
        [
            (
                "https://forge.example.com/org/repo/pull/7",
                "https://forge.example.com/org/repo/pull/7",
            ),
            (
                "HTTPS://user:example-secret@Forge.Example.com:8443/org/repo/pull/7",
                "https://forge.example.com:8443/org/repo/pull/7",
            ),
            (
                "http://forge.example.com/-/merge_requests/3?private_token=x#note",
                "http://forge.example.com/-/merge_requests/3",
            ),
            (
                "https://example-secret@forge.example.com/a",
                "https://forge.example.com/a",
            ),
        ],
    )
    def test_userinfo_query_and_fragment_are_dropped(self, stored, shown):
        assert plain_web_url(stored) == shown

    @pytest.mark.parametrize(
        "stored",
        [
            "javascript:alert(1)",
            "ftp://forge.example.com/x",
            "file:///etc/example",
            "https://",
            "https:///path-only",
            "https://forge example.com/x",
            "https://forge.example.com/x y",
            "https://forge.example.com/\x00",
            "https://[2001:db8::1]/x",
            "https://-bad-.example.com/x",
            "https://forge.example.com:99999/x",
            "https://forge.example.com/" + "a" * 2100,
            "",
            7,
            None,
            ["https://forge.example.com/x"],
        ],
    )
    def test_anything_else_is_refused(self, stored):
        assert plain_web_url(stored) is None


class TestSecurityCountIsIndexServed:
    """The Security-tier count never walks the whole table (99% auth skew)."""

    def _analyzed_sqlite(self, tmp_path):
        store = AuditLogService(tmp_path / "plan.db")
        rows = [
            make_event(
                ts=f"2026-09-01T10:{i // 60 % 60:02d}:{i % 60:02d}+00:00",
                action_type=("token_refresh_success", "authentication_success")[i % 2],
                target_type="auth",
            )
            for i in range(3000)
        ]
        rows += [
            make_event(ts=_ts(1), action_type="security_incident", target_type="auth")
        ]
        rows += [make_event(ts=_ts(2), action_type="user_deleted", target_type="user")]
        seed(store, rows)
        conn = store._get_connection()
        conn.execute("ANALYZE")
        conn.commit()
        return store, conn

    def test_sqlite_plan_has_no_full_table_scan(self, tmp_path):
        from code_indexer.server.services.audit_log_query import (
            SQLITE_DIALECT,
            build_count_sql,
        )

        _, conn = self._analyzed_sqlite(tmp_path)
        sql, params = build_count_sql(
            AuditFilters(), "security", SQLITE_DIALECT, cap=AUDIT_COUNT_CAP
        )
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
        assert not any(step.startswith("SCAN TABLE audit_logs") for step in plan), plan
        assert not any(step == "SCAN audit_logs" for step in plan), plan

    def test_split_count_is_exact(self, tmp_path):
        store, _ = self._analyzed_sqlite(tmp_path)
        page = query_audit_log(store, tier="security")
        assert page.total == 2 and page.total_capped is False
        assert _actions(page) == {"security_incident", "user_deleted"}


class TestValidation:
    @pytest.mark.parametrize(
        "cursor",
        [
            "not-base64!!",
            base64.urlsafe_b64encode(b"[1, 2]").decode(),
            base64.urlsafe_b64encode(b'["x", 2]').decode(),
            base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00", -1]').decode(),
            base64.urlsafe_b64encode(b'["2026-01-01T00:00:00+00:00", true]').decode(),
            base64.urlsafe_b64encode(b'{"a": 1}').decode(),
            "A" * 600,
        ],
    )
    def test_malformed_cursor_is_refused(self, store, cursor):
        with pytest.raises(AuditQueryError):
            query_audit_log(store, cursor=cursor)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"tier": "everything"},
            {"direction": "sideways"},
            {"direction": "newer"},
            {"legacy_offset": -1},
            {
                "cursor": encode_cursor("2026-01-01T00:00:00+00:00", 1),
                "legacy_offset": 0,
            },
            {"aggregate": True, "tier": "security"},
            {"aggregate": True, "tier": "auth_activity", "legacy_offset": 0},
            {
                "filters": build_filters(date_from="2026-01-01"),
                "all_time": True,
            },
        ],
    )
    def test_bad_mode_is_refused(self, store, kwargs):
        with pytest.raises(AuditQueryError):
            query_audit_log(store, **kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"outcome": "maybe"},
            {"source": "carrier-pigeon"},
            {"actor": "x" * 256},
            {"date_from": "yesterday"},
            {"date_from": "2026-09-02", "date_to": "2026-09-01"},
            {"actor": 5},
        ],
    )
    def test_bad_filter_is_refused(self, kwargs):
        with pytest.raises(AuditQueryError):
            build_filters(**kwargs)

    def test_offset_is_clamped_to_the_maximum(self, store):
        seed(store, [make_event(ts=_ts(1))])
        page = query_audit_log(store, legacy_offset=AUDIT_LOG_MAX_OFFSET * 10)
        assert page.rows == () and page.has_newer is True

    def test_backends_satisfy_the_read_protocol(self, store):
        from code_indexer.server.storage.postgres.audit_log_backend import (
            AuditLogPostgresBackend,
        )
        from code_indexer.server.storage.protocols import AuditLogBackend

        assert isinstance(store, AuditLogBackend)
        assert isinstance(AuditLogPostgresBackend(pool=object()), AuditLogBackend)
