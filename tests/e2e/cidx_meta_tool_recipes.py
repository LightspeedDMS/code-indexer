"""A catalog recipe for every MCP tool that takes a repository.

``repository_tools()`` selects every ``TOOL_REGISTRY`` tool whose input schema
names a repository: a top-level parameter the dispatcher's own
``_recognized_repo_param_names`` recognises, or any field, at any depth, whose
name or description is about repositories (``schema_repository_tools``).
The suite fails when a selected tool has no entry here (a new tool cannot
ship uncovered) or an entry names a tool that no longer takes a repository.
Each tool is:

- ``door``: covered by the named door or scenario test of the suite;
- ``call``: called by the restricted caller against the catalog with these
  arguments; the reply must name nothing hidden and, unless ``may_succeed``,
  must be refused.  A ``may_succeed`` recipe must succeed showing ``shows``
  (and the admin control must show ``admin_shows``), or, where the catalog
  has nothing to show, answer exactly what the admin control answers
  (``same_as_admin``);
- ``exempt``: not called, with the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Mapping, Tuple

from tests.e2e.cidx_meta_doors import GIT_UNAVAILABLE_WATCH_LOG
from tests.e2e.cidx_meta_world import CATALOG, CATALOG_GLOBAL
from tests.e2e.log_audit_claims import LogClaim, exact, pattern
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY

_REPOSITORY_NAME = re.compile(
    r"(^|_)(repo|repos|repository|repositories|alias|aliases|golden)(_|$)",
    re.IGNORECASE,
)
_REPOSITORY_DESCRIPTION = re.compile(r"repositor|\brepos?\b", re.IGNORECASE)
_NESTED_SCHEMA_KEYS = ("items", "additionalProperties")
_VARIANT_SCHEMA_KEYS = ("anyOf", "oneOf", "allOf")


@dataclass(frozen=True)
class ToolRecipe:
    kind: str
    note: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    may_succeed: bool = False
    shows: str = ""
    admin_shows: str = ""
    same_as_admin: bool = False
    logs: Tuple[LogClaim, ...] = ()
    optional_logs: Tuple[LogClaim, ...] = ()


def door(name: str) -> ToolRecipe:
    return ToolRecipe("door", note=name)


def call(
    logs: Tuple[LogClaim, ...] = (),
    optional_logs: Tuple[LogClaim, ...] = (),
    **args: Any,
) -> ToolRecipe:
    """A call the restricted caller must be refused.  ``logs``: diagnostics
    the call must log; ``optional_logs``: asynchronous ones claimed if the
    call logged them."""
    return ToolRecipe("call", args=args, logs=logs, optional_logs=optional_logs)


def shows(visible: str, admin_shows: str = "", **args: Any) -> ToolRecipe:
    """A call that succeeds for the restricted caller showing *visible*; the
    admin control succeeds showing *admin_shows* (default *visible*)."""
    return ToolRecipe(
        "call",
        args=args,
        may_succeed=True,
        shows=visible,
        admin_shows=admin_shows or visible,
    )


def same_as_admin(note: str, **args: Any) -> ToolRecipe:
    """A call that succeeds with exactly the admin control's answer."""
    return ToolRecipe(
        "call", note=note, args=args, may_succeed=True, same_as_admin=True
    )


def exempt(reason: str) -> ToolRecipe:
    return ToolRecipe("exempt", note=reason)


def repository_fields(schema: Any, where: str = "") -> List[str]:
    """Every field path in *schema*, at any depth, whose name or description
    is about repositories."""
    found: List[str] = []
    if not isinstance(schema, Mapping):
        return found
    properties = schema.get("properties")
    for name, sub in properties.items() if isinstance(properties, Mapping) else ():
        path = f"{where}.{name}" if where else str(name)
        description = (
            str(sub.get("description", "")) if isinstance(sub, Mapping) else ""
        )
        if _REPOSITORY_NAME.search(str(name)) or _REPOSITORY_DESCRIPTION.search(
            description
        ):
            found.append(path)
        found.extend(repository_fields(sub, path))
    for key in _NESTED_SCHEMA_KEYS:
        found.extend(repository_fields(schema.get(key), f"{where}[]"))
    for key in _VARIANT_SCHEMA_KEYS:
        for variant in schema.get(key) or ():
            found.extend(repository_fields(variant, where))
    return found


def schema_repository_tools(registry: Mapping[str, Any]) -> FrozenSet[str]:
    """Every tool of *registry* with a repository-bearing schema field."""
    return frozenset(
        name
        for name, tool in registry.items()
        if repository_fields((tool or {}).get("inputSchema"))
    )


def repository_tools() -> FrozenSet[str]:
    """Every registered MCP tool that names a repository: recognised by the
    dispatcher, or found by the schema audit."""
    from code_indexer.server.mcp.protocol import _recognized_repo_param_names
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    dispatcher = {
        name
        for name, tool in TOOL_REGISTRY.items()
        if set((tool.get("inputSchema") or {}).get("properties", {}))
        & set(_recognized_repo_param_names(name))
    }
    return frozenset(dispatcher) | schema_repository_tools(TOOL_REGISTRY)


_G: Dict[str, Any] = {"repository_alias": CATALOG_GLOBAL}
_HIDDEN_PATH = f"{CANARY}.md"
_NO_SCIP_INDEX = "the catalog has no SCIP index: the admin gets the same empty answer"
_WRITES_OWN_CONTENT = (
    "writes caller-authored content instead of reading the catalog; a write "
    "naming a hidden repository is covered by "
    "test_hidden_catalog_writes_share_one_refusal"
)

RECIPES: Dict[str, ToolRecipe] = {
    # Doors and scenarios of the suite.
    "activate_repository": door("test_catalog_activation_is_refused"),
    "browse_directory": door("mcp_browse_directory"),
    "cidx_quick_reference": door("test_quick_reference_counts_only_visible_domains"),
    "create_file": door("test_hidden_catalog_writes_share_one_refusal"),
    "delete_file": door("test_hidden_catalog_writes_share_one_refusal"),
    "enter_write_mode": door("test_hidden_catalog_writes_share_one_refusal"),
    "exit_write_mode": door("test_hidden_catalog_writes_share_one_refusal"),
    "depmap_find_consumers": door("mcp_depmap_find_consumers"),
    "depmap_get_domain_summary": door("mcp_depmap_domain_summary"),
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
    "create_memory": exempt(_WRITES_OWN_CONTENT),
    "edit_memory": exempt(_WRITES_OWN_CONTENT),
    "store_xray_pattern": exempt(_WRITES_OWN_CONTENT),
    # Admin-only tools: the restricted caller is refused.
    "add_golden_repo": call(url="file:///nonexistent-e2e-source", alias=CATALOG),
    "add_golden_repo_index": call(alias=CATALOG, index_type="fts"),
    "admin_embedding_stats_query": call(golden_repo_alias=CATALOG),
    "bulk_add_provider_index": call(
        provider="voyage-ai", filter="category:e2e-absent-category"
    ),
    "change_golden_repo_branch": call(alias=CATALOG, branch="main"),
    "manage_group_repos": call(
        action="add", group_id="e2e-absent-group", repos=[CATALOG]
    ),
    "manage_provider_indexes": call(action="status", **_G),
    "query_audit_logs": call(target_type="repository", target_id=CATALOG),
    "refresh_golden_repo": call(alias=CATALOG),
    # Activation-owned aliases: the restricted caller has no catalog one.
    "deactivate_repository": call(
        user_alias=CATALOG,
        logs=(
            exact(
                "code_indexer.server.mcp.handlers.repos",
                "deactivate_repository failed: Activated repository 'cidx-meta' "
                "not found for user 'e2e-catalog-viewer'",
            ),
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
            exact(
                "code_indexer.server.mcp.handlers.files",
                "[MCP-GENERAL-050] File edit failed - permission denied: Repo "
                "'cidx-meta-global' requires write mode. Call "
                "enter_write_mode('cidx-meta-global') before performing file "
                "operations.",
            ),
        ),
        # The refused edit still starts an auto-watch on the catalog clone
        # (stopped by exit_write_mode in the hidden-writes scenario).
        optional_logs=(GIT_UNAVAILABLE_WATCH_LOG,),
        **_G,
    ),
    # X-Ray family.
    "analyze_graph": call(**_G),
    "xray_dump_ast": call(file_path=f"{ALPHA}.md", **_G),
    "xray_explore": call(pattern=ALPHA, search_target="content", **_G),
    "xray_search_batch": call(
        scans=[{"driver_regex": ALPHA, "search_target": "content"}], **_G
    ),
    # Repository metadata.  The global catalog alias is not an activation:
    # these two answer "not found" (the admin too).
    "check_hnsw_health": call(**_G),
    "get_index_status": call(
        logs=(
            pattern(
                "code_indexer.server.mcp.handlers.admin",
                r"\[MCP-GENERAL-062\] get_index_status repo not found in \d+ms: "
                r"Repository directory not found: \S+/activated-repos/"
                r"e2e-catalog-viewer/cidx-meta-global",
            ),
        ),
        optional_logs=(GIT_UNAVAILABLE_WATCH_LOG,),
        **_G,
    ),
    "get_branches": call(**_G),
    "get_golden_repo_indexes": shows("semantic", alias=CATALOG),
    "repository_status": shows(f"local://{CATALOG}", alias=CATALOG_GLOBAL),
    "list_repositories": shows(ALPHA, admin_shows=CANARY),
    # SCIP on the catalog.
    **{
        tool: same_as_admin(_NO_SCIP_INDEX, symbol="alpha_entry", **_G)
        for tool in (
            "scip_context",
            "scip_definition",
            "scip_dependencies",
            "scip_dependents",
            "scip_impact",
            "scip_references",
        )
    },
    "scip_callchain": same_as_admin(
        _NO_SCIP_INDEX, from_symbol="alpha_entry", to_symbol=ALPHA, **_G
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
    "update_pull_request": call(number=1, title="x", **_G),
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
    "git_search_diffs": call(search_string=ALPHA, **_G),
    "git_show_commit": call(commit_hash="HEAD", **_G),
    "git_stage": call(file_paths=[_HIDDEN_PATH], **_G),
    "git_stash": call(action="list", **_G),
    "git_status": call(**_G),
    "git_unstage": call(file_paths=[_HIDDEN_PATH], **_G),
}
