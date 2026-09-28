"""Phase 3 E2E: security-sensitive account and credential actions are audited.

Each capability family is driven once through a real front door and its row
is read back through MCP ``query_audit_logs``:

- user creation and deletion (REST), performed by a second admin whose name
  is not the seeded one, so the recorded actor is provably the caller;
- an admin minting and revoking an MCP credential for another user (REST);
- self-service API key creation and deletion (MCP).

The actor is always the authenticated caller -- never the target -- and no
secret (password, client secret, API key) appears in any returned entry.

REST/MCP front door only -- no CLI, no direct DB access.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

_PASSWORD = "E2e-Audit-Pass-9431!"


def _entries(
    client: TestClient, headers: dict, action_type: str, actor: str
) -> List[Dict[str, Any]]:
    resp = call_mcp_tool(
        client,
        "query_audit_logs",
        {"limit": 100, "action_type": action_type, "user": actor},
        headers,
    )
    assert resp.status_code == 200, resp.text[:300]
    result = parse_mcp_result(resp.json())
    assert result.get("success") is True, result
    entries: List[Dict[str, Any]] = result["entries"]
    return entries


def _one_row(
    client: TestClient, headers: dict, action_type: str, actor: str, target_id: str
) -> Dict[str, Any]:
    rows = [
        e
        for e in _entries(client, headers, action_type, actor)
        if e["target_id"] == target_id
    ]
    assert len(rows) == 1, (action_type, rows)
    return rows[0]


def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text[:300]
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_account_and_credential_rows_name_the_caller(
    test_client: TestClient, auth_headers: dict
) -> None:
    suffix = uuid.uuid4().hex[:8]
    second_admin = f"e2e-audit-admin-{suffix}"
    member = f"e2e-audit-member-{suffix}"

    created_admin = test_client.post(
        "/api/admin/users",
        json={"username": second_admin, "password": _PASSWORD, "role": "admin"},
        headers=auth_headers,
    )
    assert created_admin.status_code == 201, created_admin.text[:300]
    admin2 = _login(test_client, second_admin, _PASSWORD)

    try:
        # --- Users (REST), performed by the second admin ---
        created = test_client.post(
            "/api/admin/users",
            json={"username": member, "password": _PASSWORD, "role": "normal_user"},
            headers=admin2,
        )
        assert created.status_code == 201, created.text[:300]
        row = _one_row(test_client, auth_headers, "user_created", second_admin, member)
        assert (row["outcome"], row["source"]) == ("success", "rest")

        # --- MCP credential for another user (REST admin door) ---
        minted = test_client.post(
            f"/api/admin/users/{member}/mcp-credentials",
            json={"name": "e2e audit"},
            headers=admin2,
        )
        assert minted.status_code == 201, minted.text[:300]
        credential = minted.json()
        revoked = test_client.delete(
            f"/api/admin/users/{member}/mcp-credentials/{credential['credential_id']}",
            headers=admin2,
        )
        assert revoked.status_code == 200, revoked.text[:300]
        for action in ("mcp_credential_created", "mcp_credential_revoked"):
            row = _one_row(
                test_client,
                auth_headers,
                action,
                second_admin,
                credential["credential_id"],
            )
            assert (row["outcome"], row["source"]) == ("success", "rest")

        # --- API key (MCP door, self-service) ---
        key_resp = call_mcp_tool(
            test_client, "create_api_key", {"description": "e2e audit"}, admin2
        )
        key = parse_mcp_result(key_resp.json())
        assert key.get("success") is True, key
        deleted_resp = call_mcp_tool(
            test_client, "delete_api_key", {"key_id": key["key_id"]}, admin2
        )
        assert parse_mcp_result(deleted_resp.json()).get("success") is True
        for action in ("api_key_created", "api_key_deleted"):
            row = _one_row(
                test_client, auth_headers, action, second_admin, key["key_id"]
            )
            assert (row["outcome"], row["source"]) == ("success", "mcp")

        # --- User deletion (REST) ---
        deleted = test_client.delete(f"/api/admin/users/{member}", headers=admin2)
        assert deleted.status_code == 200, deleted.text[:300]
        row = _one_row(test_client, auth_headers, "user_deleted", second_admin, member)
        assert row["outcome"] == "success"

        # No secret reaches any returned entry.
        every_entry = json.dumps(
            [
                _entries(test_client, auth_headers, action, second_admin)
                for action in (
                    "user_created",
                    "user_deleted",
                    "mcp_credential_created",
                    "mcp_credential_revoked",
                    "api_key_created",
                    "api_key_deleted",
                )
            ]
        )
        for secret in (_PASSWORD, credential["client_secret"], key["api_key"]):
            assert secret not in every_entry
    finally:
        test_client.delete(f"/api/admin/users/{member}", headers=auth_headers)
        test_client.delete(f"/api/admin/users/{second_admin}", headers=auth_headers)
