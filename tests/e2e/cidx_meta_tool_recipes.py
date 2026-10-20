"""A catalog recipe for every MCP tool that takes a repository.

``repository_tools()`` enumerates ``TOOL_REGISTRY`` with the dispatcher's own
``_recognized_repo_param_names``; the suite fails when a selected tool has no
entry here (a new tool cannot ship uncovered) or an entry names a tool that
no longer takes a repository.  Each tool is:

- ``door``: covered by the named door or scenario test of the suite;
- ``call``: called by the restricted caller against the catalog with these
  arguments; the reply must name nothing hidden and, unless ``may_succeed``,
  must be refused;
- ``exempt``: not called, with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Tuple

from tests.e2e.cidx_meta_world import CATALOG, CATALOG_GLOBAL
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY


@dataclass(frozen=True)
class ToolRecipe:
    kind: str
    note: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    may_succeed: bool = False
    logs: Tuple[str, ...] = ()
    optional_logs: Tuple[str, ...] = ()


def door(name: str) -> ToolRecipe:
    return ToolRecipe("door", note=name)


def call(
    may_succeed: bool = False,
    logs: Tuple[str, ...] = (),
    optional_logs: Tuple[str, ...] = (),
    **args: Any,
) -> ToolRecipe:
    """``logs``: diagnostics the call must log (claimed by exact entry);
    ``optional_logs``: asynchronous ones claimed if the call logged them."""
    return ToolRecipe(
        "call",
        args=args,
        may_succeed=may_succeed,
        logs=logs,
        optional_logs=optional_logs,
    )


def exempt(reason: str) -> ToolRecipe:
    return ToolRecipe("exempt", note=reason)


def repository_tools() -> FrozenSet[str]:
    """Every registered MCP tool whose input schema declares a parameter the
    dispatcher treats as naming a repository."""
    from code_indexer.server.mcp.protocol import _recognized_repo_param_names
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    selected = set()
    for name, tool in TOOL_REGISTRY.items():
        properties = set((tool.get("inputSchema") or {}).get("properties", {}))
        if properties & set(_recognized_repo_param_names(name)):
            selected.add(name)
    return frozenset(selected)


_G: Dict[str, Any] = {"repository_alias": CATALOG_GLOBAL}
_HIDDEN_PATH = f"{CANARY}.md"

RECIPES: Dict[str, ToolRecipe] = {
    # Doors and scenarios of the suite.
    "activate_repository": door("test_catalog_activation_is_refused"),
    "browse_directory": door("mcp_browse_directory"),
    "create_file": door("test_hidden_catalog_writes_share_one_refusal"),
    "delete_file": door("test_hidden_catalog_writes_share_one_refusal"),
    "enter_write_mode": door("test_hidden_catalog_writes_share_one_refusal"),
    "exit_write_mode": door("test_hidden_catalog_writes_share_one_refusal"),
    "depmap_find_consumers": door("mcp_depmap_find_consumers"),
    "depmap_get_repo_domains": door("mcp_depmap_repo_domains"),
    "directory_tree": door("mcp_directory_tree"),
    "get_file_content": door("mcp_get_file_content_domain"),
    "git_file_history": door("mcp_git_file_history"),
    "git_log": door("mcp_git_log"),
    "git_search_commits": door("mcp_git_search_commits"),
    "list_files": door("mcp_list_files"),
    "regex_search": door("mcp_regex_single"),
    "search_code": door("mcp_search_semantic"),
    "wiki_article_analytics": door("mcp_wiki_article_analytics"),
    "xray_search": door("mcp_xray_search"),
    # Exempt.
    "remove_golden_repo": exempt(
        "destructive admin tool (manage_golden_repos); never called on the catalog"
    ),
    "trigger_reindex": exempt(
        "starts an index job on its target instead of reading it; no catalog content"
    ),
    # Admin-only tools: the restricted caller is refused.
    "add_golden_repo": call(url="file:///nonexistent-e2e-source", alias=CATALOG),
    "add_golden_repo_index": call(alias=CATALOG, index_type="fts"),
    "admin_embedding_stats_query": call(golden_repo_alias=CATALOG),
    "change_golden_repo_branch": call(alias=CATALOG, branch="main"),
    "manage_provider_indexes": call(action="status", **_G),
    "refresh_golden_repo": call(alias=CATALOG),
    # Activation-owned aliases: the restricted caller has no catalog one.
    "deactivate_repository": call(
        user_alias=CATALOG,
        logs=(
            "deactivate_repository failed: Activated repository 'cidx-meta' "
            "not found for user 'e2e-catalog-viewer'",
        ),
    ),
    "switch_branch": call(user_alias=CATALOG, branch_name="main"),
    "sync_repository": call(user_alias=CATALOG),
    "manage_composite_repository": call(
        operation="create",
        user_alias="e2e-catalog-composite",
        golden_repo_aliases=[CATALOG, ALPHA],
    ),
    # Edits outside write mode.
    "edit_file": call(
        file_path=_HIDDEN_PATH,
        old_string="x",
        new_string="y",
        content_hash="0",
        logs=(
            "[MCP-GENERAL-050] File edit failed - permission denied: Repo "
            "'cidx-meta-global' requires write mode.",
        ),
        # The refused edit still starts an auto-watch on the catalog clone
        # (stopped by exit_write_mode in the hidden-writes scenario).
        optional_logs=("Git not available - proceeding with file-only monitoring",),
        **_G,
    ),
    # X-Ray family.
    "analyze_graph": call(**_G),
    "xray_dump_ast": call(file_path=f"{ALPHA}.md", **_G),
    "xray_explore": call(pattern=ALPHA, search_target="content", **_G),
    "xray_search_batch": call(
        scans=[{"pattern": ALPHA, "search_target": "content"}], **_G
    ),
    # Repository metadata: may answer, must name nothing hidden.
    "check_hnsw_health": call(may_succeed=True, **_G),
    "get_index_status": call(
        may_succeed=True,
        optional_logs=(
            "activated-repos/e2e-catalog-viewer/cidx-meta-global",
            "Git not available - proceeding with file-only monitoring",
        ),
        **_G,
    ),
    "get_branches": call(may_succeed=True, **_G),
    "get_golden_repo_indexes": call(may_succeed=True, alias=CATALOG),
    "repository_status": call(may_succeed=True, alias=f"{CATALOG}-global"),
    # SCIP on the catalog: no SCIP index; may answer empty.
    **{
        tool: call(may_succeed=True, symbol="alpha_entry", **_G)
        for tool in (
            "scip_context",
            "scip_definition",
            "scip_dependencies",
            "scip_dependents",
            "scip_impact",
            "scip_references",
        )
    },
    "scip_callchain": call(
        may_succeed=True, from_symbol="alpha_entry", to_symbol=ALPHA, **_G
    ),
    # CI and pull requests: the catalog has no forge remote.
    "ci_cancel_run": call(run_id="1", **_G),
    "ci_get_job_logs": call(job_id="1", **_G),
    "ci_get_run": call(run_id="1", **_G),
    "ci_list_runs": call(**_G),
    "ci_retry_run": call(run_id="1", **_G),
    "ci_search_logs": call(run_id="1", pattern=ALPHA, **_G),
    "close_pull_request": call(number=1, **_G),
    "comment_on_pull_request": call(number=1, body="x", **_G),
    "create_pull_request": call(title="x", head="a", base="main", **_G),
    "get_pull_request": call(number=1, **_G),
    "list_pull_request_comments": call(number=1, **_G),
    "list_pull_requests": call(**_G),
    "merge_pull_request": call(number=1, **_G),
    "update_pull_request": call(number=1, **_G),
    # Git history and working-tree tools on the catalog.
    "git_amend": call(**_G),
    "git_blame": call(path=f"{ALPHA}.md", **_G),
    "git_branch_create": call(branch_name="e2e-branch", **_G),
    "git_branch_delete": call(branch_name="e2e-branch", **_G),
    "git_branch_list": call(**_G),
    "git_branch_switch": call(branch_name="main", **_G),
    "git_checkout_file": call(file_path=_HIDDEN_PATH, **_G),
    "git_clean": call(**_G),
    "git_commit": call(message="x", **_G),
    "git_conflict_status": call(**_G),
    "git_diff": call(from_revision="HEAD", **_G),
    "git_fetch": call(**_G),
    "git_file_at_revision": call(path=_HIDDEN_PATH, revision="HEAD", **_G),
    "git_mark_resolved": call(file_path=_HIDDEN_PATH, **_G),
    "git_merge": call(source_branch="main", **_G),
    "git_merge_abort": call(**_G),
    "git_pull": call(**_G),
    "git_push": call(**_G),
    "git_reset": call(mode="soft", **_G),
    "git_search_diffs": call(query=CANARY, **_G),
    "git_show_commit": call(commit_hash="HEAD", **_G),
    "git_stage": call(file_paths=[_HIDDEN_PATH], **_G),
    "git_stash": call(action="list", **_G),
    "git_status": call(**_G),
    "git_unstage": call(file_paths=[_HIDDEN_PATH], **_G),
}
