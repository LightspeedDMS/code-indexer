"""Front-door adapters for the audit-log read-parity E2E tests.

One query shape is mapped onto each door's own argument names:

- Web: ``GET /admin/partials/audit-logs`` (admin web session);
- MCP: ``tools/call`` ``query_audit_logs`` (bearer token);
- REST: ``GET /api/v1/audit-logs`` (bearer token).

Each adapter returns a :class:`DoorPage` (row ids in order, ``total``,
``total_capped``, ``next_cursor``) so the doors can be compared directly.
The client is anything with httpx-style ``get`` / ``post`` (a FastAPI
``TestClient`` in Phase 3, an ``httpx.Client`` in Phase 6).

A query is a dict with ``tier``, ``date_from``, ``date_to`` and ``limit``,
plus optional ``action_type``, ``actor`` and ``cursor``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

WEB_ROWS_URL = "/admin/partials/audit-logs"
REST_URL = "/api/v1/audit-logs"
MAX_SNIPPET = 400

_TOTAL_RE = re.compile(r'data-total="(\d+)" data-total-capped="(\w+)"')
_OLDER_RE = re.compile(r'class="outline audit-page-older" hx-get="([^"]+)"')
_ROW_ID_RE = re.compile(r'data-row-id="(\d+)"')


@dataclass(frozen=True)
class DoorPage:
    """What parity compares for one page of one door."""

    ids: Tuple[int, ...]
    total: int
    total_capped: bool
    next_cursor: Optional[str]


def assert_same_page(pages: Dict[str, DoorPage]) -> DoorPage:
    """Every door returned the same page; returns it."""
    (first_door, first), *rest = pages.items()
    for door, page in rest:
        assert page == first, f"{door} differs from {first_door}: {page} != {first}"
    return first


def web_page(client: Any, query: Dict[str, Any]) -> DoorPage:
    """The Web partial (the page's own view/window vocabulary)."""
    params: Dict[str, Any] = {
        "view": query["tier"],
        "window": "custom",
        "date_from": query["date_from"],
        "date_to": query["date_to"],
        "limit": str(query["limit"]),
    }
    for name in ("action_type", "actor"):
        if query.get(name):
            params[name] = query[name]
    if query.get("cursor"):
        params.update(cursor=query["cursor"], direction="older")
    response = client.get(WEB_ROWS_URL, params=params)
    assert response.status_code == 200, response.text[:MAX_SNIPPET]
    html = response.text
    total = _TOTAL_RE.search(html)
    assert total, html[:MAX_SNIPPET]
    older = _OLDER_RE.search(html)
    cursor = None
    if older:
        cursor = parse_qs(urlsplit(unescape(older.group(1))).query)["cursor"][0]
    return DoorPage(
        ids=tuple(int(i) for i in _ROW_ID_RE.findall(html)),
        total=int(total.group(1)),
        total_capped=total.group(2) == "true",
        next_cursor=cursor,
    )


def mcp_payload(client: Any, token: str, query: Dict[str, Any]) -> Dict[str, Any]:
    """The decoded ``query_audit_logs`` result (asserts ``success``)."""
    arguments: Dict[str, Any] = {
        "tier": query["tier"],
        "from_date": query["date_from"],
        "to_date": query["date_to"],
        "limit": query["limit"],
    }
    if query.get("action_type"):
        arguments["action_type"] = query["action_type"]
    if query.get("actor"):
        arguments["user"] = query["actor"]
    if query.get("cursor"):
        arguments["cursor"] = query["cursor"]
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query_audit_logs", "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text[:MAX_SNIPPET]
    body = response.json()
    assert "result" in body, body
    payload: Dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    assert payload.get("success") is True, payload
    return payload


def mcp_page(client: Any, token: str, query: Dict[str, Any]) -> DoorPage:
    payload = mcp_payload(client, token, query)
    return DoorPage(
        ids=tuple(entry["id"] for entry in payload["entries"]),
        total=payload["total"],
        total_capped=payload["total_capped"],
        next_cursor=payload["next_cursor"],
    )


def rest_body(client: Any, token: str, query: Dict[str, Any]) -> Dict[str, Any]:
    """The REST response body (asserts HTTP 200)."""
    params: Dict[str, Any] = {
        "tier": query["tier"],
        "date_from": query["date_from"],
        "date_to": query["date_to"],
        "limit": query["limit"],
    }
    if query.get("action_type"):
        params["action_type"] = query["action_type"]
    if query.get("actor"):
        params["admin_id"] = query["actor"]
    if query.get("cursor"):
        params["cursor"] = query["cursor"]
    response = client.get(
        REST_URL, params=params, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text[:MAX_SNIPPET]
    body: Dict[str, Any] = response.json()
    return body


def rest_page(client: Any, token: str, query: Dict[str, Any]) -> DoorPage:
    body = rest_body(client, token, query)
    return DoorPage(
        ids=tuple(log["id"] for log in body["logs"]),
        total=body["total"],
        total_capped=body["total_capped"],
        next_cursor=body["next_cursor"],
    )
