"""Phase 3 — AC1: MCP Search tools respond via in-process TestClient.

Verifies that every search-category MCP tool returns a valid JSON-RPC 2.0
response shape.  Tests accept HTTP 200 (tool executed, result or error in body)
or 4xx with non-empty body (tool registered but fails for missing data/repo).
HTTP 5xx is always a failure.

Tool names sourced from tool_docs/search/*.md name fields.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.mcp_helpers import (
    DEFAULT_LIMIT,
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
# Tool name constants (search category)
# ---------------------------------------------------------------------------
TOOL_SEARCH_CODE: str = "search_code"
TOOL_REGEX_SEARCH: str = "regex_search"
TOOL_BROWSE_DIRECTORY: str = "browse_directory"
TOOL_DIRECTORY_TREE: str = "directory_tree"
TOOL_LIST_FILES: str = "list_files"
TOOL_GET_FILE_CONTENT: str = "get_file_content"
TOOL_GET_CACHED_CONTENT: str = "get_cached_content"
TOOL_WIKI_ARTICLE_ANALYTICS: str = "wiki_article_analytics"

# ---------------------------------------------------------------------------
# Tool argument key constants
# ---------------------------------------------------------------------------
ARG_KEY_QUERY_TEXT: str = "query_text"
ARG_KEY_SEARCH_MODE: str = "search_mode"
ARG_KEY_LIMIT: str = "limit"
ARG_KEY_PATTERN: str = "pattern"
ARG_KEY_PATH: str = "path"
ARG_KEY_FILE_PATH: str = "file_path"

# ---------------------------------------------------------------------------
# Tool argument value constants
# ---------------------------------------------------------------------------
ARG_QUERY_TEXT_FUNCTION: str = "test function"
ARG_QUERY_TEXT_SHORT: str = "test"
ARG_SEARCH_MODE_FTS: str = "fts"
ARG_REGEX_PATTERN: str = "def .+"
ARG_PATH_ROOT: str = "/"
ARG_FILE_README: str = "README.md"

# ---------------------------------------------------------------------------
# Parametrize table: (label, tool_name, arguments)
# Labels are unique per-case test IDs — they are not repeated literals.
# ---------------------------------------------------------------------------
SEARCH_TOOLS: list[tuple[str, str, JsonArgs]] = [
    (
        "search_code_semantic",
        TOOL_SEARCH_CODE,
        {ARG_KEY_QUERY_TEXT: ARG_QUERY_TEXT_FUNCTION, ARG_KEY_LIMIT: DEFAULT_LIMIT},
    ),
    (
        "search_code_fts",
        TOOL_SEARCH_CODE,
        {
            ARG_KEY_QUERY_TEXT: ARG_QUERY_TEXT_SHORT,
            ARG_KEY_SEARCH_MODE: ARG_SEARCH_MODE_FTS,
            ARG_KEY_LIMIT: DEFAULT_LIMIT,
        },
    ),
    (
        "regex_search",
        TOOL_REGEX_SEARCH,
        {ARG_KEY_PATTERN: ARG_REGEX_PATTERN, ARG_KEY_LIMIT: DEFAULT_LIMIT},
    ),
    (
        "browse_directory",
        TOOL_BROWSE_DIRECTORY,
        {ARG_KEY_PATH: ARG_PATH_ROOT},
    ),
    (
        "directory_tree",
        TOOL_DIRECTORY_TREE,
        {},
    ),
    (
        "list_files",
        TOOL_LIST_FILES,
        {},
    ),
    (
        "get_file_content",
        TOOL_GET_FILE_CONTENT,
        {ARG_KEY_FILE_PATH: ARG_FILE_README},
    ),
    (
        "get_cached_content",
        TOOL_GET_CACHED_CONTENT,
        {ARG_KEY_FILE_PATH: ARG_FILE_README},
    ),
    (
        "wiki_article_analytics",
        TOOL_WIKI_ARTICLE_ANALYTICS,
        {},
    ),
]


@pytest.mark.parametrize(
    PARAMETRIZE_FIELDS,
    SEARCH_TOOLS,
    ids=[t[TOOL_LABEL_INDEX] for t in SEARCH_TOOLS],
)
def test_mcp_search_tool(
    label: str,
    tool: str,
    params: JsonArgs,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each MCP search tool returns a valid JSON-RPC 2.0 response.

    Accepts HTTP 200 (tool executed) or 4xx with non-empty body (tool
    registered but fails due to missing repo/index — informative error).
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
