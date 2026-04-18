"""Phase 3 — AC5: MCP Git tools respond via in-process TestClient.

Verifies that every Git-category MCP tool returns a valid JSON-RPC 2.0
response shape.  Tests accept HTTP 200 (tool executed) or 4xx with a
non-empty body (tool registered but fails for missing/invalid data).
HTTP 5xx is always a failure.

Tool names sourced from tool_docs/git/*.md name fields.
cidx-meta is used as the repository_alias because it is always present
on a freshly started test server.  Many git operations will return 4xx
on cidx-meta (not a real git workspace) which is acceptable — the goal
is to verify the MCP tool surface exists and responds cleanly.
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
# Shared repository alias used for all git tool calls
# ---------------------------------------------------------------------------
REPO_ALIAS: str = "cidx-meta"

# ---------------------------------------------------------------------------
# Parametrize table: (label, tool_name, arguments)
# ---------------------------------------------------------------------------
GIT_TOOLS: list[tuple[str, str, JsonArgs]] = [
    # Read-only status / inspection tools
    (
        "git_status",
        "git_status",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_diff",
        "git_diff",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_log",
        "git_log",
        {"repository_alias": REPO_ALIAS, "limit": 3},
    ),
    (
        "git_show_commit",
        "git_show_commit",
        {"repository_alias": REPO_ALIAS, "commit_sha": "HEAD"},
    ),
    (
        "git_blame",
        "git_blame",
        {"repository_alias": REPO_ALIAS, "file_path": "README.md"},
    ),
    (
        "git_file_history",
        "git_file_history",
        {"repository_alias": REPO_ALIAS, "file_path": "README.md"},
    ),
    (
        "git_file_at_revision",
        "git_file_at_revision",
        {"repository_alias": REPO_ALIAS, "file_path": "README.md", "revision": "HEAD"},
    ),
    (
        "git_search_commits",
        "git_search_commits",
        {"repository_alias": REPO_ALIAS, "query": "initial"},
    ),
    (
        "git_search_diffs",
        "git_search_diffs",
        {"repository_alias": REPO_ALIAS, "query": "test"},
    ),
    # Branch tools
    (
        "git_branch_list",
        "git_branch_list",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_branch_create",
        "git_branch_create",
        {"repository_alias": REPO_ALIAS, "branch_name": "e2e-test-branch"},
    ),
    (
        "git_branch_switch",
        "git_branch_switch",
        {"repository_alias": REPO_ALIAS, "branch_name": "main"},
    ),
    (
        "git_branch_delete",
        "git_branch_delete",
        {"repository_alias": REPO_ALIAS, "branch_name": "e2e-test-branch"},
    ),
    # Staging / commit tools
    (
        "git_stage",
        "git_stage",
        {"repository_alias": REPO_ALIAS, "paths": ["README.md"]},
    ),
    (
        "git_unstage",
        "git_unstage",
        {"repository_alias": REPO_ALIAS, "paths": ["README.md"]},
    ),
    (
        "git_commit",
        "git_commit",
        {"repository_alias": REPO_ALIAS, "message": "e2e test commit"},
    ),
    (
        "git_amend",
        "git_amend",
        {"repository_alias": REPO_ALIAS},
    ),
    # Remote operations (typically fail without remote — 4xx is acceptable)
    (
        "git_fetch",
        "git_fetch",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_pull",
        "git_pull",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_push",
        "git_push",
        {"repository_alias": REPO_ALIAS},
    ),
    # Reset / clean
    (
        "git_reset",
        "git_reset",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_clean",
        "git_clean",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_checkout_file",
        "git_checkout_file",
        {"repository_alias": REPO_ALIAS, "file_path": "README.md"},
    ),
    # Merge / conflict tools
    (
        "git_merge",
        "git_merge",
        {"repository_alias": REPO_ALIAS, "branch_name": "main"},
    ),
    (
        "git_merge_abort",
        "git_merge_abort",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_conflict_status",
        "git_conflict_status",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "git_mark_resolved",
        "git_mark_resolved",
        {"repository_alias": REPO_ALIAS, "file_path": "README.md"},
    ),
    # Stash
    (
        "git_stash",
        "git_stash",
        {"repository_alias": REPO_ALIAS},
    ),
    # Pull-request tools (require GitHub remote — 4xx is acceptable)
    (
        "list_pull_requests",
        "list_pull_requests",
        {"repository_alias": REPO_ALIAS},
    ),
    (
        "get_pull_request",
        "get_pull_request",
        {"repository_alias": REPO_ALIAS, "pr_number": 1},
    ),
    (
        "list_pull_request_comments",
        "list_pull_request_comments",
        {"repository_alias": REPO_ALIAS, "pr_number": 1},
    ),
    (
        "create_pull_request",
        "create_pull_request",
        {
            "repository_alias": REPO_ALIAS,
            "title": "e2e test pr",
            "body": "e2e",
            "head": "e2e-test-branch",
            "base": "main",
        },
    ),
    (
        "comment_on_pull_request",
        "comment_on_pull_request",
        {"repository_alias": REPO_ALIAS, "pr_number": 1, "body": "e2e test comment"},
    ),
    (
        "update_pull_request",
        "update_pull_request",
        {"repository_alias": REPO_ALIAS, "pr_number": 1},
    ),
    (
        "merge_pull_request",
        "merge_pull_request",
        {"repository_alias": REPO_ALIAS, "pr_number": 1},
    ),
    (
        "close_pull_request",
        "close_pull_request",
        {"repository_alias": REPO_ALIAS, "pr_number": 1},
    ),
]


@pytest.mark.parametrize(
    PARAMETRIZE_FIELDS,
    GIT_TOOLS,
    ids=[t[TOOL_LABEL_INDEX] for t in GIT_TOOLS],
)
def test_mcp_git_tool(
    label: str,
    tool: str,
    params: JsonArgs,
    test_client: TestClient,
    auth_headers: dict,
) -> None:
    """Each MCP git tool returns a valid JSON-RPC 2.0 response.

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
