"""Phase 6 E2E: audit attribution parity on the PostgreSQL-backed server.

Same assertions as Phase 3's test_25_audit_capture_attribution.py, driven
through the REST/MCP front door of the PG-backed uvicorn:

1. ``POST /auth/login`` as admin (JSON body).
2. One failed login with a neutral username.
3. ``POST /api/v1/groups`` (an existing audited action).
4. MCP ``query_audit_logs`` returns the attributed rows.
5. ``GET /health`` reports zero dropped audit records.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, List

import httpx

from tests.e2e.helpers import require_postgres, rest_call

_NEUTRAL_USERNAME = "e2e-pg-audit-nobody"


def setup_module(_: Any) -> None:
    require_postgres()


def _audit_entries(
    client: httpx.Client, token: str, **filters: Any
) -> List[Dict[str, Any]]:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "query_audit_logs",
                "arguments": {"limit": 100, **filters},
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text[:300]
    result = json.loads(resp.json()["result"]["content"][0]["text"])
    assert result.get("success") is True, result
    entries: List[Dict[str, Any]] = result["entries"]
    return entries


def test_pg_login_failure_and_group_rows_are_attributed(
    pg_http_client: httpx.Client, pg_admin_token: str
) -> None:
    admin_user = os.environ["E2E_ADMIN_USER"]
    login = pg_http_client.post(
        "/auth/login",
        json={"username": admin_user, "password": os.environ["E2E_ADMIN_PASS"]},
    )
    assert login.status_code == 200, login.text[:300]
    failed = pg_http_client.post(
        "/auth/login",
        json={"username": _NEUTRAL_USERNAME, "password": "not-the-password"},
    )
    assert failed.status_code == 401

    created = rest_call(
        pg_http_client,
        "POST",
        "/api/v1/groups",
        pg_admin_token,
        json={
            "name": f"e2e-pg-audit-{uuid.uuid4().hex[:8]}",
            "description": "audit attribution e2e",
        },
    )
    assert created.status_code == 201, created.text[:300]
    group_id = str(created.json()["id"])
    response_correlation_id = created.headers.get("X-Correlation-ID")

    try:
        success = _audit_entries(
            pg_http_client,
            pg_admin_token,
            action_type="authentication_success",
            user=admin_user,
        )
        assert success, "no authentication_success row for the admin login"
        assert success[0]["source"] == "rest"
        assert success[0]["outcome"] == "success"
        assert success[0]["auth_method"]
        assert success[0]["event_uuid"]

        # A name that matches no account is never stored (placeholder actor).
        assert (
            _audit_entries(
                pg_http_client,
                pg_admin_token,
                action_type="authentication_failure",
                user=_NEUTRAL_USERNAME,
            )
            == []
        )
        failure = _audit_entries(
            pg_http_client,
            pg_admin_token,
            action_type="authentication_failure",
            user="(unknown)",
        )
        failed_correlation_id = failed.headers.get("X-Correlation-ID")
        if failed_correlation_id:
            failure = [
                e for e in failure if e["correlation_id"] == failed_correlation_id
            ]
            assert len(failure) == 1, failure
        assert failure, "no authentication_failure row for the failed login"
        assert failure[0]["auth_method"] == "none"

        group_rows = [
            e
            for e in _audit_entries(
                pg_http_client, pg_admin_token, action_type="group_create"
            )
            if e["target_id"] == group_id
        ]
        assert len(group_rows) == 1, group_rows
        assert group_rows[0]["source"] == "rest"
        assert group_rows[0]["event_uuid"]
        if response_correlation_id:
            assert group_rows[0]["correlation_id"] == response_correlation_id

        health = rest_call(pg_http_client, "GET", "/health", pg_admin_token)
        assert health.status_code == 200
        assert health.json()["audit"]["records_dropped_since_boot"] == 0
    finally:
        rest_call(
            pg_http_client, "DELETE", f"/api/v1/groups/{group_id}", pg_admin_token
        )
