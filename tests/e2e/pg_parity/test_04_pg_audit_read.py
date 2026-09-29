"""Phase 6 E2E: audit-log read parity on the PostgreSQL-backed server.

The MCP and REST legs of Phase 3's
``test_26_audit_log_page_and_read_parity.py``, driven through the front door
of the PG-backed uvicorn:

1. Create three groups via REST (three audited ``group_create`` rows).
2. MCP ``query_audit_logs`` and REST ``GET /api/v1/audit-logs`` return the
   same row ids in the same order, the same ``total`` / ``total_capped`` /
   ``next_cursor``, and the same second page when each door's own cursor is
   followed.
3. REST ``details`` is a JSON string; MCP ``details`` is the decoded object.
4. A malformed cursor is refused: REST 400, MCP ``success: false``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List

import httpx
import pytest

from tests.e2e.audit_read_parity_helpers import (
    assert_same_page,
    mcp_page,
    mcp_payload,
    rest_body,
    rest_page,
)
from tests.e2e.helpers import require_postgres, rest_call

_GROUPS = 3
_PAGE = 2


def setup_module(_: Any) -> None:
    require_postgres()


@pytest.fixture(scope="module")
def seeded_query(
    pg_http_client: httpx.Client, pg_admin_token: str
) -> Iterator[Dict[str, Any]]:
    """Three group_create rows inside an explicit UTC window."""
    start = datetime.now(timezone.utc) - timedelta(seconds=1)
    group_ids: List[int] = []
    try:
        for _ in range(_GROUPS):
            created = rest_call(
                pg_http_client,
                "POST",
                "/api/v1/groups",
                pg_admin_token,
                json={
                    "name": f"e2e-pg-audit-read-{uuid.uuid4().hex[:8]}",
                    "description": "audit read parity e2e",
                },
            )
            assert created.status_code == 201, created.text[:300]
            group_ids.append(int(created.json()["id"]))
        yield {
            "tier": "security",
            "action_type": "group_create",
            "actor": os.environ["E2E_ADMIN_USER"],
            "date_from": start.isoformat(),
            "date_to": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            "limit": _PAGE,
            "group_ids": group_ids,
        }
    finally:
        for group_id in group_ids:
            rest_call(
                pg_http_client, "DELETE", f"/api/v1/groups/{group_id}", pg_admin_token
            )


def test_pg_mcp_and_rest_return_the_same_pages(
    pg_http_client: httpx.Client, pg_admin_token: str, seeded_query: Dict[str, Any]
) -> None:
    first = assert_same_page(
        {
            "mcp": mcp_page(pg_http_client, pg_admin_token, seeded_query),
            "rest": rest_page(pg_http_client, pg_admin_token, seeded_query),
        }
    )
    assert len(first.ids) == _PAGE and first.total == _GROUPS
    assert first.total_capped is False and first.next_cursor

    older = {**seeded_query, "cursor": first.next_cursor}
    second = assert_same_page(
        {
            "mcp": mcp_page(pg_http_client, pg_admin_token, older),
            "rest": rest_page(pg_http_client, pg_admin_token, older),
        }
    )
    assert len(second.ids) == _GROUPS - _PAGE and second.next_cursor is None
    assert not set(first.ids) & set(second.ids)

    targets = {
        log["target_id"]
        for query in (seeded_query, older)
        for log in rest_body(pg_http_client, pg_admin_token, query)["logs"]
    }
    assert targets == {str(g) for g in seeded_query["group_ids"]}


def test_pg_rest_details_is_a_string_and_mcp_details_is_decoded(
    pg_http_client: httpx.Client, pg_admin_token: str, seeded_query: Dict[str, Any]
) -> None:
    query = {**seeded_query, "limit": 100}
    rest_logs = {
        log["id"]: log
        for log in rest_body(pg_http_client, pg_admin_token, query)["logs"]
    }
    entries = {
        e["id"]: e
        for e in mcp_payload(pg_http_client, pg_admin_token, query)["entries"]
    }
    assert set(rest_logs) == set(entries) and rest_logs
    for row_id, log in rest_logs.items():
        assert isinstance(log["details"], str)
        assert entries[row_id]["details"] == json.loads(log["details"])


def test_pg_malformed_cursor_is_refused_on_both_doors(
    pg_http_client: httpx.Client, pg_admin_token: str
) -> None:
    rest = rest_call(
        pg_http_client,
        "GET",
        "/api/v1/audit-logs",
        pg_admin_token,
        params={"cursor": "not-a-cursor"},
    )
    assert rest.status_code == 400, rest.text[:300]
    mcp = pg_http_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "query_audit_logs",
                "arguments": {"cursor": "not-a-cursor"},
            },
        },
        headers={"Authorization": f"Bearer {pg_admin_token}"},
    )
    assert mcp.status_code == 200, mcp.text[:300]
    result = json.loads(mcp.json()["result"]["content"][0]["text"])
    assert result.get("success") is False and result.get("error"), result
