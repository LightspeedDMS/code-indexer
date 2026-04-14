"""Guide and analytics handlers for CIDX MCP server.

Covers: quick reference, first-time user guide, tool categories,
Langfuse trace start/end, and wiki article analytics.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Any, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.services.config_service import get_config_service

from . import _utils
from ._utils import (
    _coerce_int,
    _get_wiki_enabled_repos,
    _mcp_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wiki Analytics constants
# ---------------------------------------------------------------------------

# Minimum search query length to trigger CIDX search filter
_WIKI_ANALYTICS_MIN_QUERY_LENGTH = 2
# Max CIDX results to use as path filter (100 is sufficient for wiki article lists)
_WIKI_ANALYTICS_MAX_SEARCH_RESULTS = 100


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_langfuse_section(
    config: Any, golden_repo_manager: Any
) -> Optional[Dict[str, Any]]:
    """
    Build Langfuse trace search section for quick reference (Story #169).

    Args:
        config: ServerConfig object with langfuse_config
        golden_repo_manager: GoldenRepoManager instance for listing repos

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

    # Get project count
    pull_projects = getattr(langfuse, "pull_projects", [])
    project_count = len(pull_projects) if pull_projects else 0

    # Build compact 4-field section (Story #222 TODO 10)
    return {
        "description": "Langfuse AI traces indexed for semantic search.",
        "search_pattern": "search_code('query', repository_alias='langfuse_<project>_<userId>')",
        "available_repositories": langfuse_repos,
        "configured_projects_count": project_count,
    }


def _build_dependency_map_section(cidx_meta_path: Path) -> str:
    """
    Build dependency map section for quick reference (Story #194).

    Args:
        cidx_meta_path: Path to cidx-meta directory

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
    domain_count = len(domain_files)

    # Return compact string (Story #222 TODO 11, enhanced Story #194)
    return (
        f"Dependency map: cidx-meta/dependency-map/ ({domain_count} domains). "
        "Shows which repos collaborate in each domain and how they interact "
        "(shared APIs, data flows, integration points). "
        "Best starting point when topic spans multiple repos. "
        "Search cidx-meta-global or read _index.md for domain list."
    )


def _get_wiki_cache_for_handler():
    """Return a WikiCache instance using golden_repo_manager.db_path.

    Instantiates WikiCache on each call (lightweight - no I/O until a method
    is called). Returns None when golden_repo_manager is unavailable.
    """
    from code_indexer.server.wiki.wiki_cache import WikiCache

    grm = getattr(_utils.app_module, "golden_repo_manager", None)
    if grm is None:
        return None
    db_path = getattr(grm, "db_path", None)
    if not db_path:
        return None
    return WikiCache(db_path)


def _wiki_analytics_filter_by_search(
    repo_alias: str, search_query: str, search_mode: str, username: str
) -> Optional[set]:
    """Filter wiki article paths via CIDX search (AC4).

    Returns a set of matching file_paths, or None if search was not performed.
    Returns an empty set when search runs but finds no matches.
    Raises RuntimeError if semantic_query_manager is unavailable.
    """
    if not search_query or len(search_query) < _WIKI_ANALYTICS_MIN_QUERY_LENGTH:
        return None
    sqm = getattr(_utils.app_module, "semantic_query_manager", None)
    if sqm is None:
        raise RuntimeError("semantic_query_manager not available for search filter")
    result = sqm.query_user_repositories(
        username=username,
        query_text=search_query,
        repository_alias=repo_alias,
        search_mode=search_mode,
        limit=_WIKI_ANALYTICS_MAX_SEARCH_RESULTS,
        file_extensions=[".md"],
    )
    search_results = result.get("results", [])
    return {r.get("file_path", "") for r in search_results}


def _wiki_analytics_build_articles(all_views: list, wiki_alias: str) -> list:
    """Transform view count records into article response dicts (AC2).

    Derives human-readable title from filename: hyphens and underscores become
    spaces, result is title-cased. Strips .md extension from wiki_url path.
    """
    articles = []
    for v in all_views:
        path = v["article_path"]
        path_no_ext = path[:-3] if path.endswith(".md") else path
        last_segment = path_no_ext.rsplit("/", 1)[-1]
        title = last_segment.replace("-", " ").replace("_", " ").title()
        articles.append(
            {
                "title": title,
                "path": path,
                "real_views": v["real_views"],
                "first_viewed_at": v["first_viewed_at"],
                "last_viewed_at": v["last_viewed_at"],
                "wiki_url": f"/wiki/{wiki_alias}/{path_no_ext}",
            }
        )
    return articles


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------


def quick_reference(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Generate quick reference documentation for available MCP tools.

    Args:
        params: {"category": str|null} - Optional category filter
        user: Authenticated user

    Returns:
        Dictionary with tool summaries filtered by category and user permissions
    """
    try:
        from ..tools import TOOL_REGISTRY
        from ..tool_doc_loader import _get_tool_doc_loader

        category_filter = params.get("category")

        # Load tool docs from singleton (Story #222 code review Finding 1: avoid per-call disk I/O)
        loader = _get_tool_doc_loader()
        all_docs = loader._cache

        # Build grouped tools_by_category dict (Story #222 TODO 9)
        tools_by_category: Dict[str, list] = {}
        total_tools = 0

        for tool_name, tool_def in TOOL_REGISTRY.items():
            # Check permission
            required_permission = tool_def.get("required_permission", "query_repos")
            if not user.has_permission(required_permission):
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

        # Story #22: Add server identity with a.k.a. line
        config = get_config_service().get_config()
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

        # Story #194: Add dependency map section when available (prominent positioning)
        if _utils.app_module.golden_repo_manager:
            cidx_meta_path = (
                Path(_utils.app_module.golden_repo_manager.golden_repos_dir)
                / "cidx-meta"
            )
            dependency_map_section = _build_dependency_map_section(cidx_meta_path)
            if dependency_map_section:
                response["dependency_map"] = dependency_map_section

        # Conditionally add Langfuse section
        langfuse_section = _build_langfuse_section(
            config, _utils.app_module.golden_repo_manager
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
                "description": "Use global_repo_status() to see what indexes exist for a repository.",
                "example_call": "global_repo_status('backend-global')",
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
        ],
        "quick_start_summary": [
            "1. whoami() - Check your permissions",
            "2. list_global_repos() - Find available repositories",
            "3. search_code('topic', 'cidx-meta-global') - Discover which repo has your topic",
            "4. global_repo_status('repo-global') - Check index capabilities",
            "5. search_code('query', 'repo-global', limit=5) - Search code",
            "6. browse_directory('repo-global', 'src') - Explore structure",
            "7. scip_definition('symbol', 'repo-global') - Find definitions",
            "8. activate_repository('repo-global', 'my-repo') - Enable editing",
            "9. edit_file -> git_stage -> git_commit -> git_push - Make changes",
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
                "solution": "Temporal indexing may not be enabled. Check with global_repo_status() - look for enable_temporal: true.",
            },
            {
                "error": "SCIP definition/references returns no results",
                "solution": "SCIP indexes may not exist for this repository. Check global_repo_status() for SCIP availability.",
            },
            {
                "error": "Repository 'cidx-meta-global' not found",
                "solution": "cidx-meta-global may not be configured on this server. Use list_global_repos() to see available repos, then search with repository_alias='*-global' or search individual repos by name.",
            },
        ],
    }

    return _mcp_response({"success": True, "guide": guide})


def get_tool_categories(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_tool_categories tool - returns tools organized by category.

    Uses ToolDocLoader singleton to build categories from markdown documentation
    files without per-call disk I/O (Story #222 code review Finding 1).
    """
    from ..tool_doc_loader import _get_tool_doc_loader

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
            # Format as "tool_name - tl_dr description"
            category_tools.append(f"{tool_info['name']} - {tool_info['tl_dr']}")
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
                extra={"correlation_id": get_correlation_id()},
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
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"status": "error", "message": str(e)})


def handle_wiki_article_analytics(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Query wiki article view analytics (Story #293).

    AC2: Returns title, path, real_views, first_viewed_at, last_viewed_at, wiki_url.
    AC3: sort_by most_viewed=DESC, least_viewed=ASC; tie-break alphabetical by path.
    AC4: Optional search_query filters via CIDX; results still sorted by views.
    AC5: Returns explicit error for non-wiki-enabled repos.
    AC6: Permission enforced by MCP middleware via tool doc required_permission.
    """
    try:
        repo_alias = params.get("repo_alias", "")
        sort_by = params.get("sort_by", "most_viewed")
        if sort_by not in ("most_viewed", "least_viewed"):
            sort_by = "most_viewed"
        limit = _coerce_int(params.get("limit"), 20)
        limit = max(1, min(limit, 500))
        search_query = params.get("search_query")
        search_mode = params.get("search_mode", "semantic")

        # Strip -global suffix to get base alias for wiki_enabled check
        wiki_alias = repo_alias[:-7] if repo_alias.endswith("-global") else repo_alias

        # AC5: Reject non-wiki-enabled repos with explicit error
        if wiki_alias not in _get_wiki_enabled_repos():
            return _mcp_response(
                {
                    "success": False,
                    "error": "Wiki is not enabled for this repository",
                }
            )

        # AC4: Optional search filter - raises on sqm unavailability
        article_paths_filter = _wiki_analytics_filter_by_search(
            repo_alias, search_query or "", search_mode, user.username
        )
        if article_paths_filter is not None and not article_paths_filter:
            return _mcp_response(
                {
                    "success": True,
                    "articles": [],
                    "total_count": 0,
                    "repo_alias": repo_alias,
                    "sort_by": sort_by,
                    "wiki_enabled": True,
                }
            )

        wiki_cache = _get_wiki_cache_for_handler()
        if wiki_cache is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Wiki cache not available",
                }
            )

        all_views = wiki_cache.get_all_view_counts(wiki_alias)

        if article_paths_filter is not None:
            all_views = [
                v for v in all_views if v["article_path"] in article_paths_filter
            ]

        # AC3: Sort with alphabetical tie-breaking
        if sort_by == "least_viewed":
            all_views.sort(key=lambda x: (x["real_views"], x["article_path"]))
        else:
            all_views.sort(key=lambda x: (-x["real_views"], x["article_path"]))

        all_views = all_views[:limit]
        articles = _wiki_analytics_build_articles(all_views, wiki_alias)

        return _mcp_response(
            {
                "success": True,
                "articles": articles,
                "total_count": len(articles),
                "repo_alias": repo_alias,
                "sort_by": sort_by,
                "wiki_enabled": True,
            }
        )

    except Exception as e:
        logger.warning(
            format_error_log(
                "WIKI-ANALYTICS-001",
                f"Error in handle_wiki_article_analytics: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _register(registry: dict) -> None:
    """Register guide and analytics handlers into HANDLER_REGISTRY."""
    registry["cidx_quick_reference"] = quick_reference
    registry["first_time_user_guide"] = first_time_user_guide
    registry["get_tool_categories"] = get_tool_categories
    registry["start_trace"] = handle_start_trace
    registry["end_trace"] = handle_end_trace
    registry["wiki_article_analytics"] = handle_wiki_article_analytics
