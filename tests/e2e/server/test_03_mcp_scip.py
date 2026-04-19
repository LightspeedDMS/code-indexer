"""Phase 3 — AC3: MCP SCIP tools respond via in-process TestClient.

Verifies that SCIP code-intelligence MCP tools return a valid JSON-RPC 2.0
response shape.  Tests accept HTTP 200 (tool executed) or 4xx with non-empty
body (tool registered but fails for missing SCIP index).  HTTP 5xx is failure.

Tool names sourced from tool_docs/scip/*.md name fields.
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
# Tool name constants (scip category)
# ---------------------------------------------------------------------------
TOOL_SCIP_DEFINITION: str = "scip_definition"
TOOL_SCIP_REFERENCES: str = "scip_references"
TOOL_SCIP_DEPENDENCIES: str = "scip_dependencies"
TOOL_SCIP_DEPENDENTS: str = "scip_dependents"
TOOL_SCIP_IMPACT: str = "scip_impact"
TOOL_SCIP_CONTEXT: str = "scip_context"
TOOL_SCIP_CALLCHAIN: str = "scip_callchain"
TOOL_SCIP_CLEANUP_STATUS: str = "scip_cleanup_status"
TOOL_SCIP_CLEANUP_HISTORY: str = "scip_cleanup_history"
TOOL_SCIP_CLEANUP_WORKSPACES: str = "scip_cleanup_workspaces"
TOOL_SCIP_PR_HISTORY: str = "scip_pr_history"

# ---------------------------------------------------------------------------
# Argument key and value constants
# ---------------------------------------------------------------------------
ARG_KEY_REPOSITORY_ALIAS: str = "repository_alias"
ARG_KEY_SYMBOL: str = "symbol"
ARG_KEY_FROM_SYMBOL: str = "from_symbol"
ARG_KEY_TO_SYMBOL: str = "to_symbol"
ARG_ALIAS_CIDX_META: str = "cidx-meta"
ARG_SYMBOL_TEST: str = "test"

# ---------------------------------------------------------------------------
# Parametrize table: (label, tool_name, arguments)
# ---------------------------------------------------------------------------
SCIP_TOOLS: list[tuple[str, str, JsonArgs]] = [
    (
        "scip_definition",
        TOOL_SCIP_DEFINITION,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_references",
        TOOL_SCIP_REFERENCES,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_dependencies",
        TOOL_SCIP_DEPENDENCIES,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_dependents",
        TOOL_SCIP_DEPENDENTS,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_impact",
        TOOL_SCIP_IMPACT,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_context",
        TOOL_SCIP_CONTEXT,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_callchain",
        TOOL_SCIP_CALLCHAIN,
        {
            ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META,
            ARG_KEY_FROM_SYMBOL: ARG_SYMBOL_TEST,
            ARG_KEY_TO_SYMBOL: ARG_SYMBOL_TEST,
        },
    ),
    (
        "scip_cleanup_status",
        TOOL_SCIP_CLEANUP_STATUS,
        {},
    ),
    (
        "scip_cleanup_history",
        TOOL_SCIP_CLEANUP_HISTORY,
        {},
    ),
    (
        "scip_cleanup_workspaces",
        TOOL_SCIP_CLEANUP_WORKSPACES,
        {},
    ),
    (
        "scip_pr_history",
        TOOL_SCIP_PR_HISTORY,
        {ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META},
    ),
]


@pytest.mark.parametrize(
    PARAMETRIZE_FIELDS,
    SCIP_TOOLS,
    ids=[t[TOOL_LABEL_INDEX] for t in SCIP_TOOLS],
)
def test_mcp_scip_tool(
    label: str,
    tool: str,
    params: JsonArgs,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each MCP SCIP tool returns a valid JSON-RPC 2.0 response.

    Accepts HTTP 200 (tool executed) or 4xx with non-empty body (tool
    registered but fails due to missing SCIP index — informative error).
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
