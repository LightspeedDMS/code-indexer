"""Phase 3 — AC6: MCP Admin and Auth tools respond via in-process TestClient.

Verifies that every Admin-category MCP tool returns a valid JSON-RPC 2.0
response shape.  Tests accept HTTP 200 (tool executed) or 4xx with a
non-empty body (tool registered but fails for missing/invalid data).
HTTP 5xx is always a failure.

Tool names sourced from tool_docs/admin/*.md name fields.

State management: CREATE operations (user, group, api_key, mcp_credential)
appear early in the parametrize table; matching DELETE operations appear at
the end so created objects are cleaned up on a successful run.  Any rc < 500
on delete is acceptable (already-gone is fine).

Note: delete_user is not present in the admin MCP tool registry.  The e2e
user created here is cleaned up when the session-scoped TestClient tears down
its isolated data directory.
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import (
    FIELD_ERROR,
    FIELD_JSONRPC,
    FIELD_RESULT,
    HTTP_OK,
    HTTP_SERVER_ERROR,
    JsonArgs,
    MAX_ERROR_SNIPPET,
    PARAMETRIZE_FIELDS,
    TOOL_LABEL_INDEX,
    call_mcp_tool,
)

# ---------------------------------------------------------------------------
# Environment variable names — same convention as conftest.py.
# e2e-automation.sh sets these for all phases before invoking pytest.
# ---------------------------------------------------------------------------
_ENV_ADMIN_USER: str = "E2E_ADMIN_USER"


def _require_env(name: str) -> str:
    """Return the value of environment variable *name* or raise RuntimeError.

    Mirrors the helper in conftest.py so no silent fallbacks exist for
    required credentials.
    """
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Run tests via e2e-automation.sh or export the variable manually."
        )
    return value


# ---------------------------------------------------------------------------
# Constants for test data created / deleted during the run.
# _E2E_USER_PASSWORD is generated at import time so no password literal is
# embedded in source.  The authenticate MCP tool accepts an api_key
# (cidx_sk_... token format), not a plain password.
# ---------------------------------------------------------------------------
E2E_USERNAME: str = "e2e_testuser"
E2E_GROUP: str = "e2e_testgrp"
E2E_API_KEY_NAME: str = "e2e_key"
E2E_MCP_CRED_NAME: str = "e2e_cred"
_E2E_USER_PASSWORD: str = uuid.uuid4().hex

# A structurally valid but non-existent API key used for the authenticate
# tool test.  This is not a secret — it is intentionally invalid so the
# server returns a clean 4xx auth-failure rather than a 422 schema error.
_AUTHENTICATE_PLACEHOLDER_API_KEY: str = "cidx_sk_e2e_placeholder"

# ---------------------------------------------------------------------------
# Parametrize table: (label, tool_name, arguments)
# The order matters — creates come before reads, deletes come last.
# ---------------------------------------------------------------------------
ADMIN_TOOLS: list[tuple[str, str, JsonArgs]] = [
    # Authentication — uses api_key (cidx_sk_... format), not a password.
    # The placeholder key triggers a clean auth-failure 4xx, not a 422.
    (
        "authenticate",
        "authenticate",
        {
            "username": _require_env(_ENV_ADMIN_USER),
            "api_key": _AUTHENTICATE_PLACEHOLDER_API_KEY,
        },
    ),
    # User management
    (
        "create_user",
        "create_user",
        {
            "username": E2E_USERNAME,
            "password": _E2E_USER_PASSWORD,
            "role": "normal_user",
        },
    ),
    (
        "list_users",
        "list_users",
        {},
    ),
    # Group management
    (
        "create_group",
        "create_group",
        {"name": E2E_GROUP},
    ),
    (
        "list_groups",
        "list_groups",
        {},
    ),
    (
        "get_group",
        "get_group",
        {"group_name": E2E_GROUP},
    ),
    (
        "add_member_to_group",
        "add_member_to_group",
        {"group_name": E2E_GROUP, "username": "admin"},
    ),
    (
        "remove_member_from_group",
        "remove_member_from_group",
        {"group_name": E2E_GROUP, "username": "admin"},
    ),
    (
        "update_group",
        "update_group",
        {"group_name": E2E_GROUP, "description": "e2e test group"},
    ),
    # API key management
    (
        "create_api_key",
        "create_api_key",
        {"name": E2E_API_KEY_NAME},
    ),
    (
        "list_api_keys",
        "list_api_keys",
        {},
    ),
    # MCP credential management
    (
        "create_mcp_credential",
        "create_mcp_credential",
        {"name": E2E_MCP_CRED_NAME},
    ),
    (
        "list_mcp_credentials",
        "list_mcp_credentials",
        {},
    ),
    (
        "admin_list_all_mcp_credentials",
        "admin_list_all_mcp_credentials",
        {},
    ),
    (
        "admin_list_system_mcp_credentials",
        "admin_list_system_mcp_credentials",
        {},
    ),
    # Health & maintenance
    (
        "check_health",
        "check_health",
        {},
    ),
    (
        "get_maintenance_status",
        "get_maintenance_status",
        {},
    ),
    # Jobs
    (
        "get_job_statistics",
        "get_job_statistics",
        {},
    ),
    # Logs
    (
        "admin_logs_query",
        "admin_logs_query",
        {"limit": 10},
    ),
    # Delegation
    (
        "list_delegation_functions",
        "list_delegation_functions",
        {},
    ),
    # Audit logs
    (
        "query_audit_logs",
        "query_audit_logs",
        {"limit": 10},
    ),
    # Git credentials (read-only)
    (
        "list_git_credentials",
        "list_git_credentials",
        {},
    ),
    # Cleanup — delete created objects last (accept any rc < 500)
    (
        "delete_mcp_credential",
        "delete_mcp_credential",
        {"name": E2E_MCP_CRED_NAME},
    ),
    (
        "delete_api_key",
        "delete_api_key",
        {"name": E2E_API_KEY_NAME},
    ),
    (
        "delete_group",
        "delete_group",
        {"group_name": E2E_GROUP},
    ),
]


@pytest.mark.parametrize(
    PARAMETRIZE_FIELDS,
    ADMIN_TOOLS,
    ids=[t[TOOL_LABEL_INDEX] for t in ADMIN_TOOLS],
)
def test_mcp_admin_tool(
    label: str,
    tool: str,
    params: JsonArgs,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each MCP admin tool returns a valid JSON-RPC 2.0 response.

    Accepts HTTP 200 (tool executed) or 4xx with non-empty body (tool
    registered but fails due to missing/invalid context — informative error).
    Fails on HTTP 5xx (unhandled server error).
    """
    resp = call_mcp_tool(test_client, tool, params, auth_headers)
    assert resp.status_code < HTTP_SERVER_ERROR, (
        f"{label}: server error {resp.status_code} — {resp.text[:MAX_ERROR_SNIPPET]}"
    )
    if resp.status_code == HTTP_OK:
        body = resp.json()
        assert FIELD_JSONRPC in body, f"{label}: missing {FIELD_JSONRPC!r} key in response"
        assert FIELD_RESULT in body or FIELD_ERROR in body, (
            f"{label}: JSON-RPC response has neither {FIELD_RESULT!r} nor {FIELD_ERROR!r}"
        )
    else:
        # 4xx: tool must return a non-empty informative body
        assert resp.text, f"{label}: HTTP {resp.status_code} response has empty body"
