"""Phase 3 — AC4: MCP Files, SSH, and Guides tools respond via in-process TestClient.

Verifies that file-management, SSH-key, and guide MCP tools return a valid
JSON-RPC 2.0 response shape.  Tests accept HTTP 200 (tool executed) or 4xx
with non-empty body (tool registered but fails for missing data).
HTTP 5xx is always a failure.

Tool names sourced from tool_docs/files/*.md, tool_docs/ssh/*.md,
and tool_docs/guides/*.md name fields.
"""

from __future__ import annotations

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
# Tool name constants — files category
# ---------------------------------------------------------------------------
TOOL_LIST_FILES: str = "list_files"

# ---------------------------------------------------------------------------
# Tool name constants — ssh category
# ---------------------------------------------------------------------------
TOOL_CIDX_SSH_KEY_LIST: str = "cidx_ssh_key_list"
TOOL_CIDX_SSH_KEY_CREATE: str = "cidx_ssh_key_create"
TOOL_CIDX_SSH_KEY_SHOW_PUBLIC: str = "cidx_ssh_key_show_public"

# ---------------------------------------------------------------------------
# Tool name constants — guides category
# ---------------------------------------------------------------------------
TOOL_CIDX_QUICK_REFERENCE: str = "cidx_quick_reference"
TOOL_FIRST_TIME_USER_GUIDE: str = "first_time_user_guide"
TOOL_GET_TOOL_CATEGORIES: str = "get_tool_categories"

# ---------------------------------------------------------------------------
# Argument key and value constants
# ---------------------------------------------------------------------------
ARG_KEY_KEY_NAME: str = "key_name"
ARG_SSH_KEY_NAME_TEST: str = "test-key"

# ---------------------------------------------------------------------------
# Parametrize table: (label, tool_name, arguments)
# ---------------------------------------------------------------------------
OTHER_TOOLS: list[tuple[str, str, JsonArgs]] = [
    # Files
    (
        "files_list_files",
        TOOL_LIST_FILES,
        {},
    ),
    # SSH keys — read operations only (no side-effects)
    (
        "ssh_keys_list",
        TOOL_CIDX_SSH_KEY_LIST,
        {},
    ),
    (
        "ssh_key_show_public",
        TOOL_CIDX_SSH_KEY_SHOW_PUBLIC,
        {ARG_KEY_KEY_NAME: ARG_SSH_KEY_NAME_TEST},
    ),
    # Guides
    (
        "cidx_quick_reference",
        TOOL_CIDX_QUICK_REFERENCE,
        {},
    ),
    (
        "first_time_user_guide",
        TOOL_FIRST_TIME_USER_GUIDE,
        {},
    ),
    (
        "get_tool_categories",
        TOOL_GET_TOOL_CATEGORIES,
        {},
    ),
]


@pytest.mark.parametrize(
    PARAMETRIZE_FIELDS,
    OTHER_TOOLS,
    ids=[str(t[TOOL_LABEL_INDEX]) for t in OTHER_TOOLS],
)
def test_mcp_other_tool(
    label: str,
    tool: str,
    params: JsonArgs,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each MCP files/ssh/guides tool returns a valid JSON-RPC 2.0 response.

    Accepts HTTP 200 (tool executed) or 4xx with non-empty body (tool
    registered but fails due to missing data — informative error).
    Fails on HTTP 5xx (unhandled server error).
    """
    resp = call_mcp_tool(test_client, tool, params, auth_headers)
    assert resp.status_code < HTTP_SERVER_ERROR, (
        f"{label}: server error {resp.status_code} — {resp.text[:MAX_ERROR_SNIPPET]}"
    )
    if resp.status_code == HTTP_OK:
        body = resp.json()
        assert FIELD_JSONRPC in body, (
            f"{label}: missing {FIELD_JSONRPC!r} key in response"
        )
        assert FIELD_RESULT in body or FIELD_ERROR in body, (
            f"{label}: JSON-RPC response has neither {FIELD_RESULT!r} nor {FIELD_ERROR!r}"
        )
    else:
        # 4xx: tool must return a non-empty informative body
        assert resp.text, f"{label}: HTTP {resp.status_code} response has empty body"
