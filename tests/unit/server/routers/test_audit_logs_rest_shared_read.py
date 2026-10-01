"""REST ``GET /api/v1/audit-logs`` reads through the shared audit read function.

A real FastAPI app with the real audit router and a REAL audit store on
``app.state.audit_service`` (SQLite, and PostgreSQL when
``TEST_POSTGRES_DSN`` is set).  No group manager is installed: the route must
not need one.  Admin identity and elevation use the established
``dependency_overrides`` pattern (the gates themselves are covered by the
groups elevation tests).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.services.audit_log_query import encode_cursor
from tests.unit.server._audit_read_support import build_store, make_event, seed

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


@pytest.fixture(params=("sqlite", "postgres"))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Any]:
    yield from build_store(request.param, tmp_path)


@pytest.fixture
def client(store) -> Iterator[TestClient]:
    from code_indexer.server.auth.dependencies import get_current_admin_user
    from code_indexer.server.routers.groups import audit_router

    app = FastAPI()
    app.include_router(audit_router)
    app.state.audit_service = store
    admin = MagicMock()
    admin.username = "admin_user"
    admin.role = "admin"
    app.dependency_overrides[get_current_admin_user] = lambda: admin
    for route in audit_router.routes:
        if isinstance(route, APIRoute):
            for dep in route.dependencies or []:
                fn = getattr(dep, "dependency", None)
                if (
                    fn is not None
                    and getattr(fn, "__qualname__", "") == _ELEVATION_QUALNAME
                ):
                    app.dependency_overrides[fn] = lambda: None
    with TestClient(app) as test_client:
        yield test_client


def _ts(minute: int, second: int = 0) -> str:
    return f"2026-09-01T10:{minute:02d}:{second:02d}+00:00"


def _seed(store) -> None:
    seed(
        store,
        [
            make_event(ts=_ts(1), target_id="u1", source="rest"),
            make_event(ts=_ts(2), target_id="u2", ip_address="198.51.100.7"),
            make_event(ts=_ts(3), target_id="u3", correlation_id="corr-3"),
            make_event(ts=_ts(3), target_id="u4", outcome="failure"),
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
                details_json=json.dumps({"username": "u7", "user_agent": "agent"}),
            ),
        ],
    )


def _get(client: TestClient, **params) -> Dict[str, Any]:
    response = client.get("/api/v1/audit-logs", params=params)
    assert response.status_code == 200, response.text[:500]
    body: Dict[str, Any] = response.json()
    return body


def _targets(body: Dict[str, Any]) -> List[str]:
    return [log["target_id"] for log in body["logs"]]


class TestDefaults:
    def test_default_is_the_security_tier(self, client, store):
        _seed(store)
        targets = _targets(_get(client))
        assert "u7" in targets  # a promoted authentication row
        assert "u6" not in targets  # routine authentication activity
        assert {"u1", "u2", "u3", "u4"} <= set(targets)

    def test_a_target_type_without_a_tier_reads_every_row_of_that_type(
        self, client, store
    ):
        _seed(store)
        assert sorted(_targets(_get(client, target_type="auth"))) == ["u6", "u7"]

    def test_explicit_tier(self, client, store):
        _seed(store)
        assert _targets(_get(client, tier="auth_activity")) == ["u6"]
        assert len(_get(client, tier="all")["logs"]) == 6

    def test_default_page_is_100_rows_with_a_next_page_token(self, client, store):
        seed(
            store,
            [
                make_event(ts=_ts(i // 60, i % 60), target_id=f"r{i}")
                for i in range(105)
            ],
        )
        body = _get(client)
        assert len(body["logs"]) == 100
        assert body["next_cursor"] and body["has_more"] is True
        assert body["total"] == 105 and body["total_capped"] is False
        rest = _get(client, cursor=body["next_cursor"])
        assert len(rest["logs"]) == 5 and rest["next_cursor"] is None
        seen = [log["id"] for log in body["logs"] + rest["logs"]]
        assert len(seen) == len(set(seen)) == 105

    def test_limit_above_1000_is_clamped(self, client, store):
        seed(
            store,
            [
                make_event(ts=_ts(i // 60 % 60, i % 60), target_id=f"c{i}")
                for i in range(1003)
            ],
        )
        assert len(_get(client, limit=5000)["logs"]) == 1000

    def test_total_is_reported_as_capped_above_the_cap(
        self, client, store, monkeypatch
    ):
        from code_indexer.server.services import audit_log_query

        _seed(store)
        monkeypatch.setattr(audit_log_query, "AUDIT_COUNT_CAP", 2)
        body = _get(client, tier="all")
        assert body["total"] == 2 and body["total_capped"] is True


class TestFiltersAndRows:
    @pytest.mark.parametrize(
        "params, expected",
        [
            ({"target_id": "u2"}, ["u2"]),
            ({"outcome": "failure"}, ["u4"]),
            ({"source": "rest"}, ["u1"]),
            ({"ip_address": "198.51.100.7"}, ["u2"]),
            ({"correlation_id": "corr-3"}, ["u3"]),
        ],
    )
    def test_new_filters_narrow_the_rows(self, client, store, params, expected):
        _seed(store)
        assert _targets(_get(client, **params)) == expected

    def test_rows_carry_the_shared_fields_and_raw_string_details(self, client, store):
        from code_indexer.server.services.audit_log_query import AUDIT_ROW_FIELDS

        _seed(store)
        log = _get(client, target_id="u7")["logs"][0]
        assert set(AUDIT_ROW_FIELDS) <= set(log)
        assert isinstance(log["id"], int)
        assert isinstance(log["details"], str)
        assert json.loads(log["details"]) == {
            "username": "u7",
            "omitted_fields": ["user_agent"],
        }

    def test_pr_creation_details_carry_the_plain_pr_url(self, client, store):
        """REST shows the recorded PR URL in ``details`` (as it always did),
        reduced to a plain web URL -- the value MCP uses for ``resource``."""
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
                            "pr_url": "https://ci-bot:example-secret@forge."
                            "example.com/example-org/example-repo/pull/7?t=x",
                        }
                    ),
                )
            ],
        )
        body = _get(client, action_type="pr_creation_success", tier="all")
        (log,) = body["logs"]
        assert json.loads(log["details"]) == {
            "job_id": "job-7",
            "pr_url": "https://forge.example.com/example-org/example-repo/pull/7",
        }
        assert "example-secret" not in json.dumps(body)

    def test_newer_direction_walks_back(self, client, store):
        _seed(store)
        first = _get(client, tier="all", limit=2)
        second = _get(client, tier="all", limit=2, cursor=first["next_cursor"])
        back = _get(
            client,
            tier="all",
            limit=2,
            cursor=second["prev_cursor"],
            direction="newer",
        )
        assert _targets(back) == _targets(first)

    def test_offset_still_pages(self, client, store):
        _seed(store)
        everything = _targets(_get(client, tier="all"))
        assert _targets(_get(client, tier="all", limit=2, offset=2)) == everything[2:4]

    def test_aggregate(self, client, store):
        _seed(store)
        body = _get(client, tier="auth_activity", aggregate=True, all_time=True)
        assert body["logs"] == []
        assert [(g["action_type"], g["count"]) for g in body["groups"]] == [
            ("token_refresh_success", 1)
        ]
        assert body["total"] == 1 and body["all_time"] is True


class TestRefusals:
    @pytest.mark.parametrize(
        "params",
        [
            {"cursor": "not-a-cursor"},
            {"direction": "newer"},
            {"tier": "everything"},
            {"outcome": "maybe"},
            {"date_from": "yesterday"},
            {"offset": -1},
            {"offset": 2, "cursor": encode_cursor("2026-09-01T10:01:00+00:00", 1)},
            {"aggregate": True, "tier": "security"},
        ],
    )
    def test_bad_arguments_are_refused_with_400(self, client, store, params):
        response = client.get("/api/v1/audit-logs", params=params)
        assert response.status_code == 400, (params, response.status_code)
        assert response.json()["detail"]

    @pytest.mark.parametrize("offset", [0, 2])
    def test_an_explicit_offset_with_a_cursor_is_refused(self, client, store, offset):
        _seed(store)
        cursor = _get(client, tier="all", limit=2)["next_cursor"]
        response = client.get(
            "/api/v1/audit-logs", params={"cursor": cursor, "offset": offset}
        )
        assert response.status_code == 400, response.text[:300]
        assert response.json()["detail"] == "cursor and offset cannot be combined"

    def test_missing_store_is_503(self, client, store):
        client.app.state.audit_service = None  # type: ignore[attr-defined]
        response = client.get("/api/v1/audit-logs")
        assert response.status_code == 503
