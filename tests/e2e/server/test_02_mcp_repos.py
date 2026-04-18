"""Phase 3 — AC2: MCP Repos tools respond via in-process TestClient.

Verifies that key repository-management MCP tools return a valid JSON-RPC 2.0
response shape.  Tests accept HTTP 200 (tool executed) or 4xx with non-empty
body (tool registered but fails for missing repo/data).  HTTP 5xx is failure.

Tool names sourced from tool_docs/repos/*.md name fields.
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
# Tool name constants (repos category)
# ---------------------------------------------------------------------------
TOOL_LIST_REPOSITORIES: str = "list_repositories"
TOOL_LIST_GLOBAL_REPOS: str = "list_global_repos"
TOOL_GET_REPOSITORY_STATUS: str = "get_repository_status"
TOOL_GET_ALL_REPOSITORIES_STATUS: str = "get_all_repositories_status"
TOOL_GET_BRANCHES: str = "get_branches"
TOOL_GET_REPOSITORY_STATISTICS: str = "get_repository_statistics"
TOOL_GET_INDEX_STATUS: str = "get_index_status"
TOOL_GET_GLOBAL_CONFIG: str = "get_global_config"
TOOL_LIST_REPO_CATEGORIES: str = "list_repo_categories"
TOOL_GLOBAL_REPO_STATUS: str = "global_repo_status"
TOOL_CHECK_HNSW_HEALTH: str = "check_hnsw_health"

# ---------------------------------------------------------------------------
# Argument key and value constants
# ---------------------------------------------------------------------------
ARG_KEY_REPOSITORY_ALIAS: str = "repository_alias"
ARG_ALIAS_CIDX_META: str = "cidx-meta"

# ---------------------------------------------------------------------------
# Parametrize table: (label, tool_name, arguments)
# ---------------------------------------------------------------------------
REPOS_TOOLS: list[tuple[str, str, JsonArgs]] = [
    (
        "list_repositories",
        TOOL_LIST_REPOSITORIES,
        {},
    ),
    (
        "list_global_repos",
        TOOL_LIST_GLOBAL_REPOS,
        {},
    ),
    (
        "get_repository_status",
        TOOL_GET_REPOSITORY_STATUS,
        {ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META},
    ),
    (
        "get_all_repositories_status",
        TOOL_GET_ALL_REPOSITORIES_STATUS,
        {},
    ),
    (
        "get_branches",
        TOOL_GET_BRANCHES,
        {ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META},
    ),
    (
        "get_repository_statistics",
        TOOL_GET_REPOSITORY_STATISTICS,
        {ARG_KEY_REPOSITORY_ALIAS: ARG_ALIAS_CIDX_META},
    ),
    (
        "get_index_status",
        TOOL_GET_INDEX_STATUS,
        {},
    ),
    (
        "get_global_config",
        TOOL_GET_GLOBAL_CONFIG,
        {},
    ),
    (
        "list_repo_categories",
        TOOL_LIST_REPO_CATEGORIES,
        {},
    ),
    (
        "global_repo_status",
        TOOL_GLOBAL_REPO_STATUS,
        {},
    ),
    (
        "check_hnsw_health",
        TOOL_CHECK_HNSW_HEALTH,
        {},
    ),
]


@pytest.mark.parametrize(
    PARAMETRIZE_FIELDS,
    REPOS_TOOLS,
    ids=[t[TOOL_LABEL_INDEX] for t in REPOS_TOOLS],
)
def test_mcp_repos_tool(
    label: str,
    tool: str,
    params: JsonArgs,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each MCP repos tool returns a valid JSON-RPC 2.0 response.

    Accepts HTTP 200 (tool executed) or 4xx with non-empty body (tool
    registered but fails due to missing repo/data — informative error).
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
