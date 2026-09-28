"""Phase 3 E2E: audit rows are attributed end to end through the front door.

1. Log in as admin via ``POST /auth/login`` (JSON body).
2. Fail one login with a neutral username.
3. Create a group via REST (an existing audited action).
4. Read the rows back through MCP ``query_audit_logs`` and assert the
   attribution columns: actor, front door, auth method, per-event uuid and
   correlation id.
5. ``GET /health`` reports zero dropped audit records.

REST/MCP front door only -- no CLI, no direct DB access.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

_NEUTRAL_USERNAME = "e2e-audit-nobody"


def _admin_credentials() -> Dict[str, str]:
    return {
        "username": os.environ["E2E_ADMIN_USER"],
        "password": os.environ["E2E_ADMIN_PASS"],
    }


def _audit_entries(
    client: TestClient, headers: dict, **filters: Any
) -> List[Dict[str, Any]]:
    resp = call_mcp_tool(client, "query_audit_logs", {"limit": 100, **filters}, headers)
    assert resp.status_code == 200, resp.text[:300]
    result = parse_mcp_result(resp.json())
    assert result.get("success") is True, result
    entries: List[Dict[str, Any]] = result["entries"]
    return entries


def test_login_failure_and_group_rows_are_attributed(
    test_client: TestClient, auth_headers: dict
) -> None:
    creds = _admin_credentials()
    login = test_client.post("/auth/login", json=creds)
    assert login.status_code == 200, login.text[:300]

    failed = test_client.post(
        "/auth/login",
        json={"username": _NEUTRAL_USERNAME, "password": "not-the-password"},
    )
    assert failed.status_code == 401

    group_name = f"e2e-audit-{uuid.uuid4().hex[:8]}"
    created = test_client.post(
        "/api/v1/groups",
        json={"name": group_name, "description": "audit attribution e2e"},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text[:300]
    group_id = str(created.json()["id"])
    response_correlation_id = created.headers.get("X-Correlation-ID")

    try:
        success_rows = _audit_entries(
            test_client,
            auth_headers,
            action_type="authentication_success",
            user=creds["username"],
        )
        assert success_rows, "no authentication_success row for the admin login"
        success = success_rows[0]
        assert success["user"] == creds["username"]
        assert success["outcome"] == "success"
        assert success["source"] == "rest"
        assert success["auth_method"]
        assert success["event_uuid"]

        # A name that matches no account is never stored: the row's actor is
        # the fixed placeholder.  Find it by the failed request's correlation.
        assert (
            _audit_entries(
                test_client,
                auth_headers,
                action_type="authentication_failure",
                user=_NEUTRAL_USERNAME,
            )
            == []
        )
        failure_rows = _audit_entries(
            test_client,
            auth_headers,
            action_type="authentication_failure",
            user="(unknown)",
        )
        failed_correlation_id = failed.headers.get("X-Correlation-ID")
        if failed_correlation_id:
            failure_rows = [
                e for e in failure_rows if e["correlation_id"] == failed_correlation_id
            ]
            assert len(failure_rows) == 1, failure_rows
        assert failure_rows, "no authentication_failure row for the failed login"
        assert failure_rows[0]["auth_method"] == "none"
        assert failure_rows[0]["outcome"] == "failure"
        assert failure_rows[0]["source"] == "rest"

        group_rows = [
            e
            for e in _audit_entries(
                test_client, auth_headers, action_type="group_create"
            )
            if e["target_id"] == group_id
        ]
        assert len(group_rows) == 1, group_rows
        group_row = group_rows[0]
        assert group_row["source"] == "rest"
        assert group_row["auth_method"] == "jwt"
        assert group_row["event_uuid"]
        if response_correlation_id:
            assert group_row["correlation_id"] == response_correlation_id

        health = test_client.get("/health", headers=auth_headers)
        assert health.status_code == 200
        assert health.json()["audit"]["records_dropped_since_boot"] == 0
    finally:
        test_client.delete(f"/api/v1/groups/{group_id}", headers=auth_headers)
