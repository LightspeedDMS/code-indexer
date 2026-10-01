"""Phase 3 E2E: the Audit Logs page and read parity through the front doors.

1. Create three groups via REST (three audited ``group_create`` rows).
2. The Audit Logs page shell renders for an admin web session.
3. The Web rows partial, MCP ``query_audit_logs`` and REST
   ``GET /api/v1/audit-logs`` return the same row ids in the same order, the
   same ``total`` / ``total_capped`` / ``next_cursor``, and following each
   door's own cursor gives the same second page.
4. REST ``details`` is a JSON string; MCP ``details`` is the decoded object.
5. A malformed cursor is refused: REST 400, MCP ``success: false``.
6. The Group Management audit tab and its partial are gone.

REST/MCP/Web front door only -- no CLI, no direct DB access.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from tests.e2e.audit_read_parity_helpers import (
    assert_same_page,
    mcp_page,
    mcp_payload,
    rest_body,
    rest_page,
    web_page,
)
from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

_GROUPS = 3
_PAGE = 2


def _admin_user() -> str:
    return os.environ["E2E_ADMIN_USER"]


@pytest.fixture(scope="module")
def web_client(test_client: TestClient) -> TestClient:
    """A second client on the SAME app (no second lifespan) with its own
    cookie jar, logged in to the Web UI as admin."""
    client = TestClient(test_client.app, raise_server_exceptions=False)
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "login page must carry a CSRF token"
    login = client.post(
        "/login",
        data={
            "username": _admin_user(),
            "password": os.environ["E2E_ADMIN_PASS"],
            "csrf_token": match.group(1),
        },
        follow_redirects=False,
    )
    assert login.status_code == 303, login.text[:300]
    return client


@pytest.fixture(scope="module")
def seeded_query(
    test_client: TestClient, admin_token_provider: Any
) -> Iterator[Dict[str, Any]]:
    """Three group_create rows inside an explicit UTC window."""
    headers = {"Authorization": f"Bearer {admin_token_provider.get_token()}"}
    # No backward margin: the in-process server stamps rows from the same
    # clock after this instant, and a margin would admit the previous
    # test's own group_create row (same admin) into the window.
    start = datetime.now(timezone.utc)
    group_ids: List[int] = []
    try:
        for _ in range(_GROUPS):
            created = test_client.post(
                "/api/v1/groups",
                json={
                    "name": f"e2e-audit-read-{uuid.uuid4().hex[:8]}",
                    "description": "audit read parity e2e",
                },
                headers=headers,
            )
            assert created.status_code == 201, created.text[:300]
            group_ids.append(int(created.json()["id"]))
        yield {
            "tier": "security",
            "action_type": "group_create",
            "actor": _admin_user(),
            "date_from": start.isoformat(),
            "date_to": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "limit": _PAGE,
            "group_ids": group_ids,
        }
    finally:
        for group_id in group_ids:
            test_client.delete(f"/api/v1/groups/{group_id}", headers=headers)


def test_audit_logs_page_shell_renders(web_client: TestClient) -> None:
    page = web_client.get("/admin/audit-logs")
    assert page.status_code == 200, page.text[:300]
    assert "Audit Logs" in page.text
    assert 'href="/admin/audit-logs"' in page.text  # the admin nav item


def test_every_door_returns_the_same_pages(
    test_client: TestClient,
    web_client: TestClient,
    admin_token_provider: Any,
    seeded_query: Dict[str, Any],
) -> None:
    token = admin_token_provider.get_token()
    first = assert_same_page(
        {
            "web": web_page(web_client, seeded_query),
            "mcp": mcp_page(test_client, token, seeded_query),
            "rest": rest_page(test_client, token, seeded_query),
        }
    )
    assert len(first.ids) == _PAGE and first.total == _GROUPS
    assert first.total_capped is False and first.next_cursor

    older = {**seeded_query, "cursor": first.next_cursor}
    second = assert_same_page(
        {
            "web": web_page(web_client, older),
            "mcp": mcp_page(test_client, token, older),
            "rest": rest_page(test_client, token, older),
        }
    )
    assert len(second.ids) == _GROUPS - _PAGE and second.next_cursor is None
    assert not set(first.ids) & set(second.ids)

    targets = {
        log["target_id"]
        for query in (seeded_query, older)
        for log in rest_body(test_client, token, query)["logs"]
    }
    assert targets == {str(g) for g in seeded_query["group_ids"]}


def test_rest_details_is_a_string_and_mcp_details_is_decoded(
    test_client: TestClient, admin_token_provider: Any, seeded_query: Dict[str, Any]
) -> None:
    token = admin_token_provider.get_token()
    query = {**seeded_query, "limit": 100}
    rest_logs = {log["id"]: log for log in rest_body(test_client, token, query)["logs"]}
    entries = {e["id"]: e for e in mcp_payload(test_client, token, query)["entries"]}
    assert set(rest_logs) == set(entries) and rest_logs
    for row_id, log in rest_logs.items():
        assert isinstance(log["details"], str)
        assert entries[row_id]["details"] == json.loads(log["details"])


def test_malformed_cursor_is_refused_on_both_doors(
    test_client: TestClient, admin_token_provider: Any
) -> None:
    token = admin_token_provider.get_token()
    headers = {"Authorization": f"Bearer {token}"}
    rest = test_client.get(
        "/api/v1/audit-logs", params={"cursor": "not-a-cursor"}, headers=headers
    )
    assert rest.status_code == 400, rest.text[:300]
    mcp = call_mcp_tool(
        test_client, "query_audit_logs", {"cursor": "not-a-cursor"}, headers
    )
    assert mcp.status_code == 200
    result = parse_mcp_result(mcp.json())
    assert result.get("success") is False and result.get("error"), result


def test_group_management_audit_tab_is_gone(web_client: TestClient) -> None:
    groups = web_client.get("/admin/groups", params={"active_tab": "audit"})
    assert groups.status_code == 200, groups.text[:300]
    assert 'id="content-audit"' not in groups.text
    assert 'id="tab-audit"' not in groups.text
    old_partial = web_client.get("/admin/partials/groups-audit-logs")
    assert old_partial.status_code == 404
