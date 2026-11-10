"""Guide and analytics handlers for CIDX MCP server.

Covers: quick reference, first-time user guide, tool categories and
Langfuse trace start/end. (Wiki article analytics is the cidx-wiki
feature's tool: ``cidx_wiki.server.mcp.analytics``.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.telemetry.correlation_bridge import (
    get_current_correlation_id as get_correlation_id,
)
from code_indexer.server.services.cidx_meta_access import (
    Decision,
    DecisionKind,
    MetaVisibility,
    resolve_meta_visibility,
)
from code_indexer.server.services.cidx_meta_access.catalog_articles import (
    catalog_decider,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.repo_access_guard import normalize_repo_alias

from . import _utils
from ._utils import (
    _mcp_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_langfuse_section(
    config: Any, golden_repo_manager: Any, vis: MetaVisibility
) -> Optional[Dict[str, Any]]:
    """
    Build Langfuse trace search section for quick reference (Story #169).

    Args:
        config: ServerConfig object with langfuse_config
        golden_repo_manager: GoldenRepoManager instance for listing repos
        vis: the caller's visibility; a restricted caller is shown only the
            trace repositories it may see

    Returns:
        Dictionary with Langfuse search documentation when pull_enabled is True,
        None otherwise
    """
    # Check if Langfuse config exists and pull is enabled
    langfuse = getattr(config, "langfuse_config", None)
    if not langfuse or not getattr(langfuse, "pull_enabled", False):
        return None

    # Get Langfuse repos from golden repo manager
    langfuse_repos = []
    if golden_repo_manager:
        try:
            all_repos = golden_repo_manager.list_golden_repos()
            langfuse_repos = [
                r.get("alias", "")
                for r in all_repos
                if r.get("alias", "").startswith("langfuse_")
            ]
        except Exception as e:
            logger.warning(f"Failed to list golden repos for Langfuse section: {e}")
            langfuse_repos = []
    if not vis.unrestricted:
        langfuse_repos = [
            alias
            for alias in langfuse_repos
            if normalize_repo_alias(alias) in vis.visible
        ]

    # Build compact 4-field section (Story #222 TODO 10)
    section: Dict[str, Any] = {
        "description": "Langfuse AI traces indexed for semantic search.",
        "search_pattern": "search_code('query', repository_alias='langfuse_<project>_<userId>')",
        "available_repositories": langfuse_repos,
    }
    # The configured project list is global: only an unrestricted caller
    # is shown its size.
    if vis.unrestricted:
        pull_projects = getattr(langfuse, "pull_projects", [])
        section["configured_projects_count"] = (
            len(pull_projects) if pull_projects else 0
        )
    return section


def _build_dependency_map_section(
    cidx_meta_path: Path, decide: Optional[Callable[[str], Decision]]
) -> str:
    """
    Build dependency map section for quick reference (Story #194).

    Args:
        cidx_meta_path: Path to cidx-meta directory
        decide: the caller's catalog path decision (None = unrestricted);
            a restricted caller counts only the domains it may see

    Returns:
        String with dependency map documentation when conditions met,
        empty string otherwise
    """
    # Check if dependency-map directory exists
    dependency_map_dir = cidx_meta_path / "dependency-map"
    if not dependency_map_dir.is_dir():
        return ""

    # Check if _index.md exists (required for complete map)
    index_file = dependency_map_dir / "_index.md"
    if not index_file.is_file():
        return ""

    # Count domain files (exclude _index.md and files starting with _)
    domain_files = [
        f
        for f in dependency_map_dir.glob("*.md")
        if f.name != "_index.md" and not f.name.startswith("_")
    ]
    if decide is not None:
        domain_files = [
            f
            for f in domain_files
            if decide(f"dependency-map/{f.name}").kind is DecisionKind.TRANSFORM
        ]
    domain_count = len(domain_files)

    # Return compact string (Story #222 TODO 11, enhanced Story #194)
    return (
        f"Dependency map: cidx-meta/dependency-map/ ({domain_count} domains). "
        "Shows which repos collaborate in each domain and how they interact "
        "(shared APIs, data flows, integration points). "
        "Best starting point when topic spans multiple repos. "
        "Search cidx-meta-global or read _index.md for domain list."
    )


def _dependency_map_read_root(cidx_meta_path: Path) -> Path:
    """Where the dependency map is read: cidx-meta itself, or -- under a
    delegated content backend -- the synced read mirror of its shared content.

    The section is an optional hint, so a mirror that cannot sync is logged
    at ERROR and the section omitted (an empty mirror root has no map); the
    rest of the quick reference is still served.
    """
    from code_indexer.server.services.cidx_meta_content import (
        CidxMetaUnavailableError,
    )
    from code_indexer.server.services.cidx_meta_read_mirror import (
        MIRROR_DIRNAME,
        synced_read_root,
    )

    try:
        mirror_root = synced_read_root(cidx_meta_path)
    except CidxMetaUnavailableError as exc:
        logger.error("quick_reference: dependency map unavailable: %s", exc)
        return cidx_meta_path.parent / MIRROR_DIRNAME / "unavailable"
    return mirror_root if mirror_root is not None else cidx_meta_path


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


def quick_reference(
    params: Dict[str, Any],
    user: User,
    *,
    session_state: Any = None,
    tool_access_memo: Any = None,
) -> Dict[str, Any]:
    """
    Generate quick reference documentation for available MCP tools.

    Args:
        params: {"category": str|null} - Optional category filter
        user: Authenticated user

    Returns:
        Dictionary with tool summaries filtered by category and user permissions
    """
    try:
        from ..tools import TOOL_REGISTRY, tool_passes_config_gate
        from ..tool_doc_loader import _get_tool_doc_loader
        from ..tool_access import ToolAccessMemo, principal_for_tool

        if tool_access_memo is None:
            tool_access_memo = ToolAccessMemo()

        category_filter = params.get("category")

        # Story #987 AC6: 'tool' parameter takes precedence over 'category'
        requested_tool = params.get("tool")
        if requested_tool:
            effective_user = principal_for_tool(user, session_state, requested_tool)
            decision = tool_access_memo.is_allowed(requested_tool, effective_user)
            tool_def = TOOL_REGISTRY.get(requested_tool)
            if decision is False or (
                decision is None
                and tool_def is not None
                and not effective_user.has_permission(
                    tool_def.get("required_permission", "query_repos")
                )
            ):
                return _mcp_response(
                    {"success": False, "error": f"Tool '{requested_tool}' not found"}
                )
            loader = _get_tool_doc_loader()
            body = loader.get_extended_description(requested_tool)
            if body is None:
                return _mcp_response(
                    {"success": False, "error": f"Tool '{requested_tool}' not found"}
                )
            return _mcp_response(
                {"success": True, "tool": requested_tool, "body": body}
            )

        # Load tool docs from singleton (Story #222 code review Finding 1: avoid per-call disk I/O)
        loader = _get_tool_doc_loader()
        all_docs = loader.get_all_docs()  # Story #987 AC5: use public accessor

        # Build grouped tools_by_category dict (Story #222 TODO 9)
        tools_by_category: Dict[str, list] = {}
        total_tools = 0

        # Story #22 / Bug #1955 item 6: fetched BEFORE the counting loop (moved
        # up from after it) so total_tools can apply the SAME requires_config
        # gate filter_tools_by_role (the real tools/list) applies -- otherwise
        # this count silently disagreed with what tools/list actually serves
        # whenever a requires_config tool (e.g. Langfuse-gated start_trace/
        # end_trace) was disabled.
        config = get_config_service().get_config()

        for tool_name, tool_def in TOOL_REGISTRY.items():
            # Check permission (the same principal tools/list uses per tool)
            effective_user = principal_for_tool(user, session_state, tool_name)
            decision = tool_access_memo.is_allowed(tool_name, effective_user)
            required_permission = tool_def.get("required_permission", "query_repos")
            if decision is False or (
                decision is None
                and not effective_user.has_permission(required_permission)
            ):
                continue

            # Bug #1955 item 6: skip a tool whose requires_config condition
            # (e.g. langfuse_enabled) is not satisfied -- mirrors
            # filter_tools_by_role exactly (Rule 4, anti-duplication).
            if not tool_passes_config_gate(tool_def, config):
                continue

            # Get category and tl_dr from frontmatter; fallback for undocumented tools
            if tool_name in all_docs:
                doc = all_docs[tool_name]
                tool_category = doc.category
                tl_dr = doc.tl_dr
            else:
                tool_category = "other"
                tl_dr = tool_def.get("description", "")[:60]

            # Apply category filter
            if category_filter and tool_category != category_filter:
                continue

            # Truncate tl_dr to 30 chars for token budget (Story #222 AC2: standard user <= 2000 tokens
            # even when Langfuse repos with long names are configured)
            if len(tl_dr) > 30:
                tl_dr = tl_dr[:27] + "..."

            if tool_category not in tools_by_category:
                tools_by_category[tool_category] = []
            tools_by_category[tool_category].append({"name": tool_name, "tl_dr": tl_dr})
            total_tools += 1

        # Story #22: Add server identity with a.k.a. line (config fetched
        # above, before the counting loop)
        display_name = config.service_display_name or "Neo"
        server_identity = f"This server is CIDX (a.k.a. {display_name})."

        # Compact discovery string (Story #222 TODO 12)
        discovery = (
            "Use cidx-meta-global for cross-repo discovery: "
            "search_code('topic', repository_alias='cidx-meta-global'). "
            "Strip .md from file_path, append '-global' for repo alias."
        )

        # Story #169: Add Langfuse trace search section when pull is enabled
        # Omit category_filter key when null (Story #222 code review Finding 2: saves tokens)
        response: Dict[str, Any] = {
            "success": True,
            "server_identity": server_identity,
            "total_tools": total_tools,
            "tools_by_category": tools_by_category,
            "discovery": discovery,
        }
        if category_filter is not None:
            response["category_filter"] = category_filter

        access_service = _utils._get_access_filtering_service()
        vis = resolve_meta_visibility(user.username, access_service)

        # Story #194: Add dependency map section when available (prominent positioning)
        if _utils.app_module.golden_repo_manager:
            cidx_meta_path = (
                Path(_utils.app_module.golden_repo_manager.golden_repos_dir)
                / "cidx-meta"
            )
            decide = (
                None
                if vis.unrestricted
                else catalog_decider(cidx_meta_path, vis, access_service)
            )
            dependency_map_section = _build_dependency_map_section(
                _dependency_map_read_root(cidx_meta_path), decide
            )
            if dependency_map_section:
                response["dependency_map"] = dependency_map_section

        # Conditionally add Langfuse section
        langfuse_section = _build_langfuse_section(
            config, _utils.app_module.golden_repo_manager, vis
        )
        if langfuse_section:
            response["langfuse_trace_search"] = langfuse_section

        return _mcp_response(response)

    except Exception as e:
        logger.exception(
            f"Error in quick_reference: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {
                "success": False,
                "total_tools": 0,
                "category_filter": category_filter,
                "tools_by_category": {},
                "error": str(e),
            }
        )


def first_time_user_guide(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for first_time_user_guide tool - returns step-by-step onboarding guide."""
    guide = {
        "steps": [
            {
                "step_number": 1,
                "title": "Check your identity and permissions",
                "description": "Use whoami() to see your username, role, and what actions you can perform.",
                "example_call": "whoami()",
                "expected_result": "Returns your username, role (admin/normal_user), and permission list",
            },
            {
                "step_number": 2,
                "title": "Discover available repositories",
                "description": "Use list_global_repos() to see all repositories available for searching.",
                "example_call": "list_global_repos()",
                "expected_result": "List of repository aliases ending in '-global' (e.g., 'backend-global')",
            },
            {
                "step_number": 3,
                "title": "Discover which repository has your topic",
                "description": "Search cidx-meta-global to find which repository covers your topic. You'll get two types of results: (1) Repo description files like 'auth-service.md' -- strip .md and append '-global' for the repo alias. (2) Dependency-map files like 'dependency-map/authentication.md' -- these show how multiple repos collaborate in a domain and are the best starting point when your topic crosses repo boundaries.",
                "example_call": "search_code(query_text='authentication', repository_alias='cidx-meta-global', limit=5)",
                "expected_result": "Results like file_path='auth-service.md' mean search 'auth-service-global' for actual code. dependency-map/ results list participating repos, their roles, and integration points -- use these to search multiple repos together with repository_alias as an array.",
            },
            {
                "step_number": 4,
                "title": "Check repository capabilities",
                "description": "Use repository_status() to see what indexes exist for a repository.",
                "example_call": "repository_status(alias='backend-global')",
                "expected_result": "Index status showing semantic, FTS, temporal, and SCIP availability",
            },
            {
                "step_number": 5,
                "title": "Run your first search",
                "description": "Use search_code() with a conceptual query. Start with small limit to conserve tokens.",
                "example_call": "search_code(query_text='authentication', repository_alias='backend-global', limit=5)",
                "expected_result": "Code snippets with similarity scores, file paths, and line numbers",
            },
            {
                "step_number": 6,
                "title": "Explore repository structure",
                "description": "Use browse_directory() to see files and folders in a repository.",
                "example_call": "browse_directory(repository_alias='backend-global', path='src')",
                "expected_result": "List of files and directories with metadata",
            },
            {
                "step_number": 7,
                "title": "Use code intelligence (if SCIP available)",
                "description": "Use scip_definition() to find where functions/classes are defined.",
                "example_call": "scip_definition(symbol='authenticate_user', repository_alias='backend-global')",
                "expected_result": "Definition location with file path, line number, and context",
            },
            {
                "step_number": 8,
                "title": "Activate repository for editing",
                "description": "Use activate_repository() to create your personal writable workspace.",
                "example_call": "activate_repository(golden_repo_alias='backend-global', user_alias='my-backend')",
                "expected_result": "Confirmation with your new workspace alias",
            },
            {
                "step_number": 9,
                "title": "Make changes with git workflow",
                "description": "Use file CRUD and git tools: create_file/edit_file -> git_stage -> git_commit -> git_push",
                "example_call": "git_stage(repository_alias='my-backend', file_paths=['src/new_file.py'])",
                "expected_result": "Files staged for commit, ready for git_commit",
            },
            {
                "step_number": 10,
                "title": "Cross-file analysis with X-Ray (analyze_graph)",
                "description": "search_code() inspects one match at a time; xray_search inspects one file's AST at a time. For a question that spans the WHOLE repository -- dead code, unwired components, layering violations, endpoint-to-sink reachability, blast radius -- use analyze_graph(), the X-Ray graph-mode tool: it builds a real cross-file reference graph (Java/Kotlin) and runs your evaluator over it. See cidx_quick_reference(tool='analyze_graph') for the full contract, templates, and worked examples.",
                "example_call": "cidx_quick_reference(tool='analyze_graph')",
                "expected_result": "The full analyze_graph reference, including ready-to-copy evaluator templates for dead-code sweeps, endpoint-to-sink reachability, and cycle detection",
            },
        ],
        "quick_start_summary": [
            "1. whoami() - Check your permissions",
            "2. list_global_repos() - Find available repositories",
            "3. search_code('topic', 'cidx-meta-global') - Discover which repo has your topic",
            "4. repository_status(alias='repo-global') - Check index capabilities",
            "5. search_code('query', 'repo-global', limit=5) - Search code",
            "6. browse_directory('repo-global', 'src') - Explore structure",
            "7. scip_definition('symbol', 'repo-global') - Find definitions",
            "8. activate_repository('repo-global', 'my-repo') - Enable editing",
            "9. edit_file -> git_stage -> git_commit -> git_push - Make changes",
            "10. analyze_graph() - X-Ray whole-repository graph analysis (dead code, blast radius, reachability)",
        ],
        "common_errors": [
            {
                "error": "Repository 'myrepo' not found",
                "solution": "Check if you meant 'myrepo-global' (for search) or need to activate first. Use list_global_repos() and list_activated_repos() to verify.",
            },
            {
                "error": "Cannot write to global repository",
                "solution": "Global repos are read-only. Use activate_repository() first to create a writable workspace.",
            },
            {
                "error": "Permission denied: requires repository:write",
                "solution": "Check your role with whoami(). The normal_user role may not have write permissions.",
            },
            {
                "error": "Empty temporal query results",
                "solution": "Temporal indexing may not be enabled. Check with repository_status() - look for enable_temporal: true.",
            },
            {
                "error": "SCIP definition/references returns no results",
                "solution": "SCIP indexes may not exist for this repository. Check repository_status() for SCIP availability.",
            },
            {
                "error": "Repository 'cidx-meta-global' not found",
                "solution": "cidx-meta-global may not be configured on this server. Use list_global_repos() to see available repos, then search with repository_alias='*-global' or search individual repos by name.",
            },
        ],
    }

    return _mcp_response({"success": True, "guide": guide})


def get_tool_categories(
    args: Dict[str, Any],
    user: User,
    *,
    session_state: Any = None,
    tool_access_memo: Any = None,
) -> Dict[str, Any]:
    """Handler for get_tool_categories tool - returns tools organized by category.

    Uses ToolDocLoader singleton to build categories from markdown documentation
    files without per-call disk I/O (Story #222 code review Finding 1).
    """
    from ..tool_doc_loader import _get_tool_doc_loader
    from ..tools import TOOL_REGISTRY
    from ..tool_access import ToolAccessMemo, principal_for_tool

    if tool_access_memo is None:
        tool_access_memo = ToolAccessMemo()

    # Use singleton to avoid per-call disk I/O
    loader = _get_tool_doc_loader()
    tool_categories = loader.get_tools_by_category()

    # Build categorized response with formatted output
    categories = {}
    total_tools = 0

    for category_name, tools in tool_categories.items():
        # Format category name for display (uppercase)
        display_name = category_name.upper()
        category_tools = []
        for tool_info in tools:
            tool_name = tool_info["name"]
            # The same principal tools/list uses for this tool.
            effective_user = principal_for_tool(user, session_state, tool_name)
            decision = tool_access_memo.is_allowed(tool_name, effective_user)
            tool_def = TOOL_REGISTRY.get(tool_name, {})
            if decision is False or (
                decision is None
                and not effective_user.has_permission(
                    tool_def.get("required_permission", "query_repos")
                )
            ):
                continue
            # Format as "tool_name - tl_dr description"
            category_tools.append(f"{tool_name} - {tool_info['tl_dr']}")
            total_tools += 1
        if category_tools:
            categories[display_name] = category_tools

    return _mcp_response(
        {
            "success": True,
            "categories": categories,
            "total_tools": total_tools,
        }
    )


def handle_start_trace(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    Start a new Langfuse trace for the current research session.

    Args:
        args: Tool arguments containing name, optional strategy, metadata, input, tags, and intel
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for accessing session context

    Returns:
        MCP response with trace status and trace_id
    """
    try:
        from code_indexer.server.services.langfuse_service import get_langfuse_service

        service = get_langfuse_service()
        if not service.is_enabled():
            return _mcp_response(
                {"status": "disabled", "message": "Langfuse tracing is not enabled"}
            )

        if not session_state:
            return _mcp_response(
                {"status": "error", "message": "No session context available"}
            )

        session_id = session_state.session_id
        username = user.username

        # Story #185: Renamed topic to name
        name = args.get("name")
        if not name:
            return _mcp_response(
                {"status": "error", "message": "Missing required parameter: name"}
            )

        strategy = args.get("strategy")
        metadata = args.get("metadata")
        # Story #185: New parameters for prompt observability
        input_text = args.get("input")
        tags = args.get("tags")
        intel = args.get("intel")

        trace_ctx = service.trace_manager.start_trace(
            session_id=session_id,
            name=name,
            strategy=strategy,
            metadata=metadata,
            username=username,
            input=input_text,
            tags=tags,
            intel=intel,
        )

        if trace_ctx is None:
            return _mcp_response(
                {"status": "error", "message": "Failed to create trace"}
            )

        # Bug #137 fix: Return session_id so HTTP clients know which session to use
        return _mcp_response(
            {
                "status": "active",
                "trace_id": trace_ctx.trace_id,
                "session_id": session_id,
            }
        )

    except Exception as e:
        logger.error(
            format_error_log(
                "TRACE-001",
                f"Error in handle_start_trace: {e}",
            )
        )
        return _mcp_response({"status": "error", "message": str(e)})


def handle_end_trace(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    End the current active trace for the research session.

    Args:
        args: Tool arguments with optional score, summary, outcome, output, tags, and intel
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for accessing session context

    Returns:
        MCP response with trace status and trace_id
    """
    try:
        from code_indexer.server.services.langfuse_service import get_langfuse_service

        service = get_langfuse_service()
        if not service.is_enabled():
            return _mcp_response(
                {"status": "disabled", "message": "Langfuse tracing is not enabled"}
            )

        if not session_state:
            return _mcp_response(
                {"status": "error", "message": "No session context available"}
            )

        session_id = session_state.session_id
        username = user.username

        # Get trace_id before ending
        # Bug #137 fix: Pass username for fallback lookup (HTTP client support)
        trace_ctx = service.trace_manager.get_active_trace(
            session_id, username=username
        )
        if not trace_ctx:
            return _mcp_response(
                {"status": "no_active_trace", "message": "No active trace to end"}
            )

        trace_id = trace_ctx.trace_id

        score = args.get("score")
        # Story #185: Renamed feedback to summary
        summary = args.get("summary")
        outcome = args.get("outcome")
        # Story #185: New parameters for prompt observability
        output = args.get("output")
        tags = args.get("tags")
        intel = args.get("intel")

        # Bug #137 fix: Pass username for fallback lookup (HTTP client support)
        success = service.trace_manager.end_trace(
            session_id=session_id,
            score=score,
            summary=summary,
            outcome=outcome,
            username=username,
            output=output,
            tags=tags,
            intel=intel,
        )

        if success:
            return _mcp_response({"status": "ended", "trace_id": trace_id})
        else:
            return _mcp_response({"status": "error", "message": "Failed to end trace"})

    except Exception as e:
        logger.error(
            format_error_log(
                "TRACE-002",
                f"Error in handle_end_trace: {e}",
            )
        )
        return _mcp_response({"status": "error", "message": str(e)})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def dependency_analysis_workflow(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for dependency_analysis_workflow tool - returns the workflow guide body.

    Loads the markdown body of guides/dependency_analysis_workflow.md via the
    ToolDocLoader singleton's public API and returns it as structured content.
    This lets an MCP client read the two-phase dep-map workflow programmatically,
    mirroring the pattern used by first_time_user_guide and cidx_quick_reference.
    """
    from ..tool_doc_loader import _get_tool_doc_loader

    loader = _get_tool_doc_loader()
    docs = loader.load_all_docs()
    doc = docs.get("dependency_analysis_workflow")
    if doc is None:
        logger.error(
            "dependency_analysis_workflow guide missing from ToolDocLoader cache — "
            "the file guides/dependency_analysis_workflow.md is expected on disk"
        )
        return _mcp_response(
            {
                "success": False,
                "error": "dependency_analysis_workflow guide not found in tool docs cache",
                "guide": "",
            }
        )

    return _mcp_response(
        {
            "success": True,
            "name": doc.name,
            "category": doc.category,
            "tl_dr": doc.tl_dr,
            "guide": doc.description,
        }
    )


def _register(registry: dict) -> None:
    """Register guide and analytics handlers into HANDLER_REGISTRY."""
    registry["cidx_quick_reference"] = quick_reference
    registry["first_time_user_guide"] = first_time_user_guide
    registry["get_tool_categories"] = get_tool_categories
    registry["dependency_analysis_workflow"] = dependency_analysis_workflow
    registry["start_trace"] = handle_start_trace
    registry["end_trace"] = handle_end_trace
