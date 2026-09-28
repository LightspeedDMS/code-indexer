"""Read parity: the three audit-log doors return the same page.

The Web Audit Logs partial (``GET /admin/partials/audit-logs``), MCP
``query_audit_logs`` (JSON-RPC ``tools/call`` over ``POST /mcp``) and REST
``GET /api/v1/audit-logs`` are driven through their real routes on one real
app (fresh server data directory, real lifespan, real Web login and bearer
token) over one real SQLite audit store.  For the same filters every door
must return the same row ids in the same order, the same ``total`` and
``total_capped`` and the same ``next_cursor`` -- and following each door's
own cursor must give the same second page.

The page's view and window map onto the tier and date range; parity calls
pass an explicit UTC window because the page defaults to the last 7 days.
The MCP handler reads the store from the module-level app (the object
uvicorn serves), so the fixture points it at this app's store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.unit.server._audit_read_support import make_event

WINDOW_FROM = "2026-09-01T00:00:00+00:00"
WINDOW_TO = "2026-09-01T23:59:59+00:00"


def _ts(minute: int) -> str:
    return f"2026-09-01T10:{minute:02d}:00+00:00"


# Newest first.  The two rows at minute 8 share a timestamp and straddle the
# first page boundary of a 3-row page.
_SEED = (
    dict(ts=_ts(10), actor="alice", target_type="user", target_id="u10"),
    dict(ts=_ts(9), actor="bob", target_type="group", target_id="7"),
    dict(ts=_ts(8), actor="alice", target_type="user", target_id="u8a"),
    dict(ts=_ts(8), actor="alice", target_type="user", target_id="u8b"),
    dict(
        ts=_ts(7),
        actor="alice",
        action_type="password_change_success",
        target_type="auth",
        target_id="alice",
        details_json=json.dumps({"username": "alice", "user_agent": "x"}),
    ),
    dict(
        ts=_ts(6),
        actor="alice",
        action_type="token_refresh_success",
        target_type="auth",
        target_id="alice",
    ),
    dict(
        ts=_ts(5),
        actor="bob",
        action_type="golden_repo_removed",
        target_type="repo",
        target_id="example-repo",
        details_json=json.dumps({"job_id": "job-1", "note": "free text"}),
    ),
    dict(
        ts=_ts(4),
        actor="bob",
        action_type="authentication_success",
        target_type="auth",
        target_id="bob",
    ),
    dict(ts=_ts(3), actor="alice", target_type="user", target_id="u3"),
    dict(ts=_ts(2), actor="bob", target_type="user", target_id="u2"),
)


@dataclass(frozen=True)
class DoorPage:
    """What parity compares for one page of one door."""

    ids: Tuple[int, ...]
    total: int
    total_capped: bool
    next_cursor: Optional[str]


# ---------------------------------------------------------------------------
# One real app, three doors
# ---------------------------------------------------------------------------


class Doors:
    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def web(self, query: Dict[str, Any]) -> DoorPage:
        params: Dict[str, Any] = {
            "view": query["tier"],
            "window": "custom",
            "date_from": WINDOW_FROM,
            "date_to": WINDOW_TO,
            "limit": str(query["limit"]),
        }
        for name in ("actor", "target_type"):
            if query.get(name):
                params[name] = query[name]
        if query.get("cursor"):
            params.update(cursor=query["cursor"], direction="older")
        response = self.client.get("/admin/partials/audit-logs", params=params)
        assert response.status_code == 200, response.text[:400]
        html = response.text
        total = re.search(r'data-total="(\d+)" data-total-capped="(\w+)"', html)
        assert total, html[:400]
        older = re.search(r'class="outline audit-page-older" hx-get="([^"]+)"', html)
        cursor = None
        if older:
            cursor = parse_qs(urlsplit(unescape(older.group(1))).query)["cursor"][0]
        return DoorPage(
            ids=tuple(int(i) for i in re.findall(r'data-row-id="(\d+)"', html)),
            total=int(total.group(1)),
            total_capped=total.group(2) == "true",
            next_cursor=cursor,
        )

    def mcp_payload(self, query: Dict[str, Any]) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "tier": query["tier"],
            "from_date": WINDOW_FROM,
            "to_date": WINDOW_TO,
            "limit": query["limit"],
        }
        if query.get("actor"):
            arguments["user"] = query["actor"]
        if query.get("target_type"):
            arguments["target_type"] = query["target_type"]
        if query.get("cursor"):
            arguments["cursor"] = query["cursor"]
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "query_audit_logs", "arguments": arguments},
            },
            headers=self.headers,
        )
        assert response.status_code == 200, response.text[:400]
        body = response.json()
        assert "result" in body, body
        payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
        assert payload["success"] is True, payload
        return payload

    def mcp(self, query: Dict[str, Any]) -> DoorPage:
        payload = self.mcp_payload(query)
        return DoorPage(
            ids=tuple(entry["id"] for entry in payload["entries"]),
            total=payload["total"],
            total_capped=payload["total_capped"],
            next_cursor=payload["next_cursor"],
        )

    def rest_body(self, query: Dict[str, Any]) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "tier": query["tier"],
            "date_from": WINDOW_FROM,
            "date_to": WINDOW_TO,
            "limit": query["limit"],
        }
        if query.get("actor"):
            params["admin_id"] = query["actor"]
        if query.get("target_type"):
            params["target_type"] = query["target_type"]
        if query.get("cursor"):
            params["cursor"] = query["cursor"]
        response = self.client.get(
            "/api/v1/audit-logs", params=params, headers=self.headers
        )
        assert response.status_code == 200, response.text[:400]
        body: Dict[str, Any] = response.json()
        return body

    def rest(self, query: Dict[str, Any]) -> DoorPage:
        body = self.rest_body(query)
        return DoorPage(
            ids=tuple(log["id"] for log in body["logs"]),
            total=body["total"],
            total_capped=body["total_capped"],
            next_cursor=body["next_cursor"],
        )


@pytest.fixture(scope="module")
def doors(tmp_path_factory) -> Iterator[Doors]:
    data_dir = tmp_path_factory.mktemp("audit_parity_server")
    env = {
        "CIDX_SERVER_DATA_DIR": str(data_dir),
        "CIDX_DATA_DIR": str(data_dir / "cidx"),
    }
    with patch.dict("os.environ", env):
        import code_indexer.server.app as app_module
        from code_indexer.server.services.config_service import reset_config_service

        reset_config_service()
        app = app_module.create_app()
        with TestClient(app, follow_redirects=False) as client:
            store = app.state.audit_service
            store.insert_events([make_event(**row) for row in _SEED])
            module_state = app_module.app.state
            sentinel = object()
            previous = getattr(module_state, "audit_service", sentinel)
            module_state.audit_service = store
            try:
                yield Doors(client, _login(client))
            finally:
                if previous is sentinel:
                    del module_state.audit_service
                else:
                    module_state.audit_service = previous
        reset_config_service()


def _login(client: TestClient) -> str:
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "login page must carry a CSRF token"
    web = client.post(
        "/login",
        data={"username": "admin", "password": "admin", "csrf_token": match.group(1)},
    )
    assert web.status_code == 303, web.text[:300]
    rest = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert rest.status_code == 200, rest.text[:300]
    token: str = rest.json()["access_token"]
    return token


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------

QUERIES: List[Dict[str, Any]] = [
    {"tier": "security", "limit": 3},
    {"tier": "all", "actor": "alice", "limit": 2},
    {"tier": "security", "target_type": "user", "limit": 3},
    {"tier": "auth_activity", "limit": 1},
]


def assert_same_page(pages: Dict[str, DoorPage]) -> DoorPage:
    """Every door returned the same page; returns it."""
    (first_door, first), *rest = pages.items()
    for door, page in rest:
        assert page == first, f"{door} differs from {first_door}: {page} != {first}"
    return first


def _all_doors(doors: Doors, query: Dict[str, Any]) -> Dict[str, DoorPage]:
    return {"web": doors.web(query), "mcp": doors.mcp(query), "rest": doors.rest(query)}


@pytest.mark.parametrize("query", QUERIES, ids=lambda q: "-".join(map(str, q.values())))
def test_same_page_and_same_next_page_on_every_door(doors, query):
    first = assert_same_page(_all_doors(doors, query))
    assert len(first.ids) == query["limit"], first  # never a vacuous match
    assert first.next_cursor is not None
    assert first.total_capped is False

    # Follow each door's OWN cursor: the second pages must match too.
    second = assert_same_page(
        {
            "web": doors.web({**query, "cursor": first.next_cursor}),
            "mcp": doors.mcp({**query, "cursor": first.next_cursor}),
            "rest": doors.rest({**query, "cursor": first.next_cursor}),
        }
    )
    assert second.total == first.total
    assert not set(second.ids) & set(first.ids)


def test_rows_sharing_a_timestamp_across_the_page_boundary_are_kept(doors):
    query = QUERIES[0]
    first = doors.rest(query)
    second = doors.rest({**query, "cursor": first.next_cursor})
    targets = [
        log["target_id"]
        for body in (
            doors.rest_body(query),
            doors.rest_body({**query, "cursor": first.next_cursor}),
        )
        for log in body["logs"]
    ]
    # u8a and u8b share one timestamp and straddle the 3-row boundary.
    assert targets[2:4] == ["u8b", "u8a"] or targets[2:4] == ["u8a", "u8b"]
    assert len(set(first.ids + second.ids)) == len(first.ids + second.ids)


def test_security_default_leaves_out_authentication_activity(doors):
    body = doors.rest_body({"tier": "security", "limit": 100})
    actions = {log["action_type"] for log in body["logs"]}
    assert "password_change_success" in actions  # promoted
    assert not {"token_refresh_success", "authentication_success"} & actions


def test_rest_details_is_a_string_and_mcp_details_is_the_decoded_object(doors):
    query = {"tier": "all", "actor": "bob", "limit": 100}
    rest_logs = {log["id"]: log for log in doors.rest_body(query)["logs"]}
    mcp_entries = {e["id"]: e for e in doors.mcp_payload(query)["entries"]}
    assert set(rest_logs) == set(mcp_entries)
    repo_row = next(log for log in rest_logs.values() if log["target_type"] == "repo")
    assert isinstance(repo_row["details"], str)
    decoded = json.loads(repo_row["details"])
    assert decoded == {"job_id": "job-1", "omitted_fields": ["note"]}
    assert mcp_entries[repo_row["id"]]["details"] == decoded
    for row_id, log in rest_logs.items():
        entry = mcp_entries[row_id]
        expected = json.loads(log["details"]) if log["details"] else {}
        assert entry["details"] == expected


def test_every_door_exposes_the_same_row_fields(doors):
    """One defined field list: REST rows, MCP entries (plus their older
    aliases) and the page's export rows, with the same projected details."""
    from code_indexer.server.services.audit_log_query import AUDIT_ROW_FIELDS

    query = {"tier": "all", "actor": "bob", "limit": 100}
    rest_logs = doors.rest_body(query)["logs"]
    assert rest_logs and all(set(log) == set(AUDIT_ROW_FIELDS) for log in rest_logs)
    entries = doors.mcp_payload(query)["entries"]
    aliases = {"user", "action", "resource"}
    assert all(set(e) == set(AUDIT_ROW_FIELDS) | aliases for e in entries)
    export = doors.client.get(
        "/admin/audit-logs/export",
        params={
            "format": "json",
            "view": "all",
            "window": "custom",
            "date_from": WINDOW_FROM,
            "date_to": WINDOW_TO,
            "actor": "bob",
        },
    )
    assert export.status_code == 200, export.text[:300]
    exported = {row["id"]: row for row in export.json()["rows"]}
    assert all(tuple(row) == AUDIT_ROW_FIELDS for row in exported.values())
    assert {log["id"]: log["details"] for log in rest_logs} == {
        row_id: row["details"] for row_id, row in exported.items()
    }


def test_a_divergent_adapter_fails_the_parity_check(doors):
    """Negative control: a door that drops the tier (reads every row) is
    caught by the same comparison the parity tests use."""
    query = QUERIES[0]

    def divergent_mcp(q: Dict[str, Any]) -> DoorPage:
        return doors.mcp({**q, "tier": "all"})

    with pytest.raises(AssertionError, match="mcp differs from web"):
        assert_same_page(
            {"web": doors.web(query), "mcp": divergent_mcp(query), "rest": doors.rest(query)}
        )
