"""MCP Tool Handler Functions - Complete implementation for all 22 tools.

All handlers return MCP-compliant responses with content arrays:
{
    "content": [
        {
            "type": "text",
            "text": "<JSON-stringified response data>"
        }
    ]
}
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import difflib
import json
import logging
import pathspec
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from code_indexer.services.hnsw_health_service import HNSWHealthService
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth import dependencies
from code_indexer.server.utils.registry_factory import get_server_global_registry
from code_indexer.server import app as app_module
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.api_metrics_service import api_metrics_service
from code_indexer.server.services.ssh_key_manager import (
    SSHKeyManager,
    KeyNotFoundError,
    HostConflictError,
)
from code_indexer.server.services.ssh_key_generator import (
    InvalidKeyNameError,
    KeyAlreadyExistsError,
)
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
    GitCommandError,
)
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.scip_audit import SCIPAuditRepository
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Initialize SCIP Audit Repository singleton
scip_audit_repository = SCIPAuditRepository()

# Module-level singleton for HNSWHealthService (Story #59 - fix caching bug)
_hnsw_health_service: Optional["HNSWHealthService"] = None


def _get_hnsw_health_service() -> "HNSWHealthService":
    """Get or create HNSWHealthService singleton.

    Returns singleton instance with 5-minute cache TTL.
    Cache persists across requests.
    """
    global _hnsw_health_service
    if _hnsw_health_service is None:
        from code_indexer.services.hnsw_health_service import HNSWHealthService

        _hnsw_health_service = HNSWHealthService(cache_ttl_seconds=300)
    return _hnsw_health_service


def _parse_json_string_array(value: Any) -> Any:
    """Parse JSON string arrays from MCP clients that serialize arrays as strings.

    Some MCP clients send arrays as JSON strings like '["repo1", "repo2"]'
    instead of actual arrays. This function handles that case.
    """
    if isinstance(value, str) and value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return value


def _coerce_int(value: Any, default: int) -> int:
    """Coerce MCP parameter to int, returning default on failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    """Coerce MCP parameter to float, returning default on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _get_wiki_enabled_repos() -> set:
    """Build set of wiki-enabled golden repo aliases (Story #292 AC2).

    Called once per MCP request to avoid per-result DB queries.
    Returns set of alias strings without -global suffix (e.g. {"sf-kb-wiki", "docs-repo"}).
    Degrades gracefully: returns empty set on any error.
    """
    try:
        grm = getattr(app_module, "golden_repo_manager", None)
        if grm is None:
            return set()
        all_repos = grm._sqlite_backend.list_repos()
        return {
            repo["alias"]
            for repo in all_repos
            if repo.get("wiki_enabled", False)
        }
    except Exception as e:
        logger.debug("Failed to fetch wiki-enabled repos, degrading gracefully: %s", e)
        return set()


def _enrich_with_wiki_url(
    result_dict: dict,
    file_path: Any,
    repository_alias: Any,
    wiki_enabled_repos: set,
) -> None:
    """Add wiki_url to result dict for .md files from wiki-enabled golden repos (Story #292).

    Modifies result_dict in-place. Only adds wiki_url when ALL conditions are met:
    - file_path ends with .md
    - repository_alias (after stripping -global suffix) is in wiki_enabled_repos

    Field is completely omitted (not null, not empty) when conditions are not met (AC4).
    Wiki URL format: /wiki/{alias_without_global}/{path_without_md_extension}
    Example: /wiki/sf-kb-wiki/Customer/getting-started
    """
    if not file_path or not str(file_path).endswith(".md"):
        return

    # Strip -global suffix to get golden repo alias for wiki_enabled check
    wiki_alias = repository_alias
    if wiki_alias and str(wiki_alias).endswith("-global"):
        wiki_alias = str(wiki_alias)[:-7]  # Remove "-global" (7 chars)

    if not wiki_alias or wiki_alias not in wiki_enabled_repos:
        return

    # Strip .md extension and build wiki URL
    article_path = str(file_path)[:-3]  # Remove ".md"
    result_dict["wiki_url"] = f"/wiki/{wiki_alias}/{article_path}"


def _mcp_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap response data in MCP-compliant content array format.

    Per MCP spec, all tool responses must return:
    {
        "content": [
            {
                "type": "text",
                "text": "<JSON-stringified data>"
            }
        ]
    }

    Args:
        data: The actual response data to wrap (dict with success, results, etc)

    Returns:
        MCP-compliant response with content array
    """
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}


def _get_golden_repos_dir() -> str:
    """Get golden_repos_dir from app.state.

    Raises:
        RuntimeError: If golden_repos_dir is not configured in app.state
    """
    from typing import Optional, cast

    golden_repos_dir: Optional[str] = cast(
        Optional[str], getattr(app_module.app.state, "golden_repos_dir", None)
    )
    if golden_repos_dir:
        return golden_repos_dir

    raise RuntimeError(
        "golden_repos_dir not configured in app.state. "
        "Server must set app.state.golden_repos_dir during startup."
    )


def _get_query_tracker():
    """Get QueryTracker from app.state.

    Returns:
        QueryTracker instance if configured, None otherwise.
        Used for tracking active queries to prevent concurrent access issues
        during repository removal operations.
    """
    return getattr(app_module.app.state, "query_tracker", None)


def _get_app_refresh_scheduler():
    """Get RefreshScheduler from app.state via global_lifecycle_manager (Story #231).

    Returns:
        RefreshScheduler instance if configured, None otherwise.
    """
    lifecycle_manager = getattr(app_module.app.state, "global_lifecycle_manager", None)
    if lifecycle_manager is None:
        return None
    return getattr(lifecycle_manager, "refresh_scheduler", None)


def _get_scip_query_service():
    """Get SCIPQueryService instance for SCIP handlers.

    Creates a SCIPQueryService configured with:
    - golden_repos_dir: From app.state (server configuration)
    - access_filtering_service: From app.state (for user-based repository filtering)

    Returns:
        SCIPQueryService instance ready for use by SCIP handlers

    Raises:
        RuntimeError: If golden_repos_dir is not configured
    """
    from code_indexer.server.services.scip_query_service import SCIPQueryService

    golden_repos_dir = _get_golden_repos_dir()
    access_filtering_service = getattr(
        app_module.app.state, "access_filtering_service", None
    )

    return SCIPQueryService(
        golden_repos_dir=golden_repos_dir,
        access_filtering_service=access_filtering_service,
    )


def _apply_payload_truncation(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply payload truncation to search results (Story #679, Bug Fix #683).

    Story #50: Converted from async to sync for FastAPI thread pool execution.

    For results with large content, replaces content with preview + cache_handle.
    This reduces response size while allowing clients to fetch full content on demand.

    Handles both 'content' field (REST API format) and 'code_snippet' field
    (semantic search QueryResult.to_dict() format).

    Args:
        results: List of search result dicts with 'content' or 'code_snippet' field

    Returns:
        Modified results list with truncation applied
    """
    payload_cache = getattr(app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        # Cache not available, return results unchanged
        return results

    for result_dict in results:
        # Handle both content and code_snippet fields (Bug Fix #683)
        # Logic for field selection:
        # - If ONLY code_snippet exists: truncate code_snippet (semantic search format)
        # - If ONLY content exists: truncate content (REST API format)
        # - If BOTH exist: truncate content (hybrid mode - code_snippet handled by FTS)
        has_code_snippet = "code_snippet" in result_dict
        has_content = "content" in result_dict

        if has_content:
            # Content field exists - truncate it (works for both legacy and hybrid)
            content = result_dict.get("content")
            field_name = "content"
        elif has_code_snippet:
            # Only code_snippet exists - truncate it (semantic search format)
            content = result_dict.get("code_snippet")
            field_name = "code_snippet"
        else:
            # No content field to truncate, add default metadata
            result_dict["cache_handle"] = None
            result_dict["has_more"] = False
            continue

        if content is None:
            # Field exists but is None, add default metadata
            result_dict["cache_handle"] = None
            result_dict["has_more"] = False
            continue

        try:
            truncated = payload_cache.truncate_result(content)  # Sync call
            if truncated.get("has_more", False):
                # Large content: replace with preview and cache handle
                result_dict["preview"] = truncated["preview"]
                result_dict["cache_handle"] = truncated["cache_handle"]
                result_dict["has_more"] = True
                result_dict["total_size"] = truncated["total_size"]
                del result_dict[field_name]  # Remove full content
            else:
                # Small content: keep as-is, add metadata
                result_dict["cache_handle"] = None
                result_dict["has_more"] = False
        except Exception as e:
            # Log error but don't fail the search
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-023",
                    f"Failed to truncate result: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            # Keep original content on error
            result_dict["cache_handle"] = None
            result_dict["has_more"] = False

    return results


def _apply_fts_payload_truncation(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply payload truncation to FTS search results (Story #680).

    Story #50: Converted from async to sync for FastAPI thread pool execution.

    For FTS results with large code_snippet or match_text fields, replaces
    them with preview + cache_handle. Each field is cached independently.

    Args:
        results: List of FTS search result dicts with 'code_snippet' and/or
                 'match_text' fields

    Returns:
        Modified results list with truncation applied to FTS fields
    """
    payload_cache = getattr(app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        # Cache not available, return results unchanged
        return results

    preview_size = payload_cache.config.preview_size_chars

    for result_dict in results:
        # Handle code_snippet field (AC1)
        code_snippet = result_dict.get("code_snippet")
        if code_snippet is not None:
            try:
                if len(code_snippet) > preview_size:
                    # Large snippet: store and replace with preview (sync call)
                    cache_handle = payload_cache.store(code_snippet)
                    result_dict["snippet_preview"] = code_snippet[:preview_size]
                    result_dict["snippet_cache_handle"] = cache_handle
                    result_dict["snippet_has_more"] = True
                    result_dict["snippet_total_size"] = len(code_snippet)
                    del result_dict["code_snippet"]
                else:
                    # Small snippet: keep as-is, add metadata
                    result_dict["snippet_cache_handle"] = None
                    result_dict["snippet_has_more"] = False
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-024",
                        f"Failed to truncate code_snippet: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                result_dict["snippet_cache_handle"] = None
                result_dict["snippet_has_more"] = False

        # Handle match_text field (AC2)
        match_text = result_dict.get("match_text")
        if match_text is not None:
            try:
                if len(match_text) > preview_size:
                    # Large match_text: store and replace with preview (sync call)
                    cache_handle = payload_cache.store(match_text)
                    result_dict["match_text_preview"] = match_text[:preview_size]
                    result_dict["match_text_cache_handle"] = cache_handle
                    result_dict["match_text_has_more"] = True
                    result_dict["match_text_total_size"] = len(match_text)
                    del result_dict["match_text"]
                else:
                    # Small match_text: keep as-is, add metadata
                    result_dict["match_text_cache_handle"] = None
                    result_dict["match_text_has_more"] = False
            except Exception as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-025",
                        f"Failed to truncate match_text: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                result_dict["match_text_cache_handle"] = None
                result_dict["match_text_has_more"] = False

    return results


def _truncate_regex_field(
    result_dict: Dict[str, Any],
    field_name: str,
    payload_cache,
    preview_size: int,
    is_list: bool = False,
) -> None:
    """Truncate a single regex field if needed (Story #684 helper).

    Story #50: Converted from async to sync for FastAPI thread pool execution.

    Args:
        result_dict: Dict containing the field to truncate
        field_name: Name of the field (e.g., "line_content", "context_before")
        payload_cache: PayloadCache instance for storing large content
        preview_size: Maximum chars before truncation
        is_list: If True, field is a list of strings to join with newlines
    """
    field_value = result_dict.get(field_name)
    if field_value is None:
        return

    try:
        content = "\n".join(field_value) if is_list else field_value
        if len(content) > preview_size:
            cache_handle = payload_cache.store(content)  # Sync call
            result_dict[f"{field_name}_preview"] = content[:preview_size]
            result_dict[f"{field_name}_cache_handle"] = cache_handle
            result_dict[f"{field_name}_has_more"] = True
            result_dict[f"{field_name}_total_size"] = len(content)
            del result_dict[field_name]
        else:
            result_dict[f"{field_name}_cache_handle"] = None
            result_dict[f"{field_name}_has_more"] = False
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-026",
                f"Failed to truncate {field_name}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        result_dict[f"{field_name}_cache_handle"] = None
        result_dict[f"{field_name}_has_more"] = False


def _apply_regex_payload_truncation(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply payload truncation to regex search results (Story #684).

    Story #50: Converted from async to sync for FastAPI thread pool execution.

    For regex results with large line_content, context_before, or context_after
    fields, replaces them with preview + cache_handle. Each field is cached
    independently.

    Args:
        results: List of regex search result dicts

    Returns:
        Modified results list with truncation applied to regex fields
    """
    payload_cache = getattr(app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        return results

    preview_size = payload_cache.config.preview_size_chars

    for result_dict in results:
        # AC1: Handle line_content field (sync call)
        _truncate_regex_field(
            result_dict, "line_content", payload_cache, preview_size, is_list=False
        )
        # AC2: Handle context_before field (list of strings, sync call)
        _truncate_regex_field(
            result_dict, "context_before", payload_cache, preview_size, is_list=True
        )
        # AC2: Handle context_after field (list of strings, sync call)
        _truncate_regex_field(
            result_dict, "context_after", payload_cache, preview_size, is_list=True
        )

    return results


def _truncate_field(
    container: Dict[str, Any],
    field_name: str,
    payload_cache,
    preview_size: int,
    log_context: str = "field",
) -> None:
    """Truncate a single field if it exceeds preview_size (Story #681 helper).

    Story #50: Converted from async to sync for FastAPI thread pool execution.

    Args:
        container: Dict containing the field to truncate
        field_name: Name of the field (e.g., "content", "diff")
        payload_cache: PayloadCache instance for storing large content
        preview_size: Maximum chars before truncation
        log_context: Context string for warning messages
    """
    value = container.get(field_name)
    if value is None:
        return

    try:
        if len(value) > preview_size:
            cache_handle = payload_cache.store(value)  # Sync call
            container[f"{field_name}_preview"] = value[:preview_size]
            container[f"{field_name}_cache_handle"] = cache_handle
            container[f"{field_name}_has_more"] = True
            container[f"{field_name}_total_size"] = len(value)
            del container[field_name]
        else:
            container[f"{field_name}_cache_handle"] = None
            container[f"{field_name}_has_more"] = False
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-027",
                f"Failed to truncate {log_context}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        container[f"{field_name}_cache_handle"] = None
        container[f"{field_name}_has_more"] = False


def _apply_temporal_payload_truncation(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply payload truncation to temporal search results (Story #681).

    Story #50: Converted from async to sync for FastAPI thread pool execution.

    Truncates large content fields with preview + cache_handle pattern.

    Args:
        results: List of temporal search result dicts

    Returns:
        Modified results with truncation applied to content and evolution entries
    """
    payload_cache = getattr(app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        return results

    preview_size = payload_cache.config.preview_size_chars

    for result_dict in results:
        # AC1: Handle main content field (sync call)
        _truncate_field(
            result_dict, "content", payload_cache, preview_size, "temporal content"
        )

        # Handle code_snippet field (temporal results use QueryResult.to_dict() format)
        _truncate_field(
            result_dict,
            "code_snippet",
            payload_cache,
            preview_size,
            "temporal code_snippet",
        )

        # AC2/AC3: Handle temporal_context.evolution entries (sync calls)
        temporal_context = result_dict.get("temporal_context")
        if temporal_context and "evolution" in temporal_context:
            for entry in temporal_context["evolution"]:
                _truncate_field(
                    entry, "content", payload_cache, preview_size, "evolution content"
                )
                _truncate_field(
                    entry, "diff", payload_cache, preview_size, "evolution diff"
                )

    return results


def _apply_scip_payload_truncation(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply payload truncation to SCIP query results (Story #685).

    Story #50: Converted from async to sync for FastAPI thread pool execution.

    For SCIP results with large context fields (> preview_size_chars), replaces
    context with context_preview + context_cache_handle. This reduces response
    size while allowing clients to fetch full context on demand.

    Args:
        results: List of SCIP result dicts with optional 'context' field

    Returns:
        Modified results list with truncation applied to context fields
    """
    payload_cache = getattr(app_module.app.state, "payload_cache", None)
    if payload_cache is None:
        # Cache not available, return results unchanged
        return results

    preview_size = payload_cache.config.preview_size_chars

    for result_dict in results:
        context = result_dict.get("context")

        # Handle missing context field
        if "context" not in result_dict:
            result_dict["context_cache_handle"] = None
            result_dict["context_has_more"] = False
            continue

        # Handle None context
        if context is None:
            result_dict["context_cache_handle"] = None
            result_dict["context_has_more"] = False
            continue

        try:
            if len(context) > preview_size:
                # Large context: store full content and replace with preview (sync call)
                cache_handle = payload_cache.store(context)
                result_dict["context_preview"] = context[:preview_size]
                result_dict["context_cache_handle"] = cache_handle
                result_dict["context_has_more"] = True
                result_dict["context_total_size"] = len(context)
                del result_dict["context"]
            else:
                # Small context: keep as-is, add metadata
                result_dict["context_cache_handle"] = None
                result_dict["context_has_more"] = False
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-028",
                    f"Failed to truncate SCIP context: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            # Keep original context on error, add metadata
            result_dict["context_cache_handle"] = None
            result_dict["context_has_more"] = False

    return results


def _error_with_suggestions(
    error_msg: str,
    attempted_value: str,
    available_values: List[str],
    max_suggestions: int = 3,
) -> Dict[str, Any]:
    """Create structured error response with fuzzy-matched suggestions.

    Args:
        error_msg: The error message to include
        attempted_value: The value the user tried (e.g., "myrepo-gloabl")
        available_values: List of valid values to match against
        max_suggestions: Maximum number of suggestions to return

    Returns:
        Structured error envelope with suggestions and available_values
    """
    # Use difflib for fuzzy matching
    suggestions = difflib.get_close_matches(
        attempted_value,
        available_values,
        n=max_suggestions,
        cutoff=0.6,  # 60% similarity threshold
    )

    return {
        "success": False,
        "error": error_msg,
        "suggestions": suggestions,
        "available_values": available_values[:10],  # Limit to prevent huge responses
    }


def _get_available_repos() -> List[str]:
    """Get list of available global repository aliases for suggestions."""
    try:
        golden_repos_dir = _get_golden_repos_dir()
        registry = get_server_global_registry(golden_repos_dir)
        return [r["alias_name"] for r in registry.list_global_repos()]
    except Exception:
        return []


def _format_omni_response(
    all_results: List[Dict[str, Any]],
    response_format: str,
    total_repos_searched: int,
    errors: Dict[str, str],
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Format omni-search results based on response_format parameter.

    Args:
        all_results: Flat list of results with source_repo field
        response_format: "flat" or "grouped"
        total_repos_searched: Number of repos successfully searched
        errors: Dict of repo alias -> error message for failed repos
        cursor: Optional cursor for pagination

    Returns:
        Formatted response dict
    """
    base_response: Dict[str, Any] = {
        "success": True,
        "total_repos_searched": total_repos_searched,
        "errors": errors,
    }

    if cursor:
        base_response["cursor"] = cursor

    if response_format == "grouped":
        results_by_repo: Dict[str, Dict[str, Any]] = {}
        for result in all_results:
            repo = result.get("source_repo", "unknown")
            if repo not in results_by_repo:
                results_by_repo[repo] = {"count": 0, "results": []}
            results_by_repo[repo]["count"] += 1
            results_by_repo[repo]["results"].append(result)

        base_response["results_by_repo"] = results_by_repo
        base_response["total_results"] = len(all_results)
    else:
        base_response["results"] = all_results
        base_response["total_results"] = len(all_results)

    return base_response


def _is_temporal_query(params: Dict[str, Any]) -> bool:
    """Check if query includes temporal parameters.

    Returns True if any temporal search parameters are present and truthy.
    """
    temporal_params = ["time_range", "time_range_all", "at_commit", "include_removed"]
    return any(params.get(p) for p in temporal_params)


def _get_temporal_status(repo_aliases: List[str]) -> Dict[str, Any]:
    """Get temporal indexing status for each repository.

    Args:
        repo_aliases: List of repository aliases to check

    Returns:
        Dict with temporal_repos, non_temporal_repos, and optional warning
    """
    try:
        golden_repos_dir = _get_golden_repos_dir()
        registry = get_server_global_registry(golden_repos_dir)
        all_repos = {r["alias_name"]: r for r in registry.list_global_repos()}

        temporal_repos = []
        non_temporal_repos = []

        for alias in repo_aliases:
            if alias in all_repos:
                if all_repos[alias].get("enable_temporal", False):
                    temporal_repos.append(alias)
                else:
                    non_temporal_repos.append(alias)

        status: Dict[str, Any] = {
            "temporal_repos": temporal_repos,
            "non_temporal_repos": non_temporal_repos,
        }

        if not temporal_repos and non_temporal_repos:
            status["warning"] = (
                "None of the searched repositories have temporal indexing enabled. "
                "Temporal queries will return no results. "
                "Re-index with --index-commits to enable temporal search."
            )

        return status
    except Exception:
        return {}


WILDCARD_CHARS = {"*", "?", "["}


def _has_wildcard(pattern: str) -> bool:
    """Check if pattern contains wildcard characters."""
    return any(c in pattern for c in WILDCARD_CHARS)


def _validate_symbol_format(symbol: Optional[str], param_name: str) -> Optional[str]:
    """Validate symbol format for call chain queries.

    Args:
        symbol: The symbol string to validate (can be None)
        param_name: Parameter name for error messages (e.g., "from_symbol", "to_symbol")

    Returns:
        None if valid, error message string if invalid
    """
    if not symbol or not symbol.strip():
        return f"{param_name} cannot be empty"

    return None


def _expand_wildcard_patterns(patterns: List[str]) -> List[str]:
    """Expand wildcard patterns to matching repository aliases.

    Args:
        patterns: List of repo patterns (may include wildcards like '*-global')

    Returns:
        Expanded list of unique repository aliases
    """
    golden_repos_dir = _get_golden_repos_dir()
    if not golden_repos_dir:
        logger.debug(
            "No golden_repos_dir, returning patterns unchanged",
            extra={"correlation_id": get_correlation_id()},
        )
        return patterns

    # Get available repos
    try:
        registry = get_server_global_registry(golden_repos_dir)
        available_repos = [r["alias_name"] for r in registry.list_global_repos()]
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-029",
                f"Failed to list global repos for wildcard expansion: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return patterns

    expanded = []
    for pattern in patterns:
        if _has_wildcard(pattern):
            # Expand wildcard using pathspec (gitignore-style matching)
            # This correctly handles ** as "zero or more directories"
            spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
            matches = [repo for repo in available_repos if spec.match_file(repo)]
            if matches:
                logger.debug(
                    f"Expanded wildcard '{pattern}' -> {matches}",
                    extra={"correlation_id": get_correlation_id()},
                )
                expanded.extend(matches)
            else:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-030",
                        f"Wildcard pattern '{pattern}' matched no repositories",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
        else:
            # Keep literal pattern
            expanded.append(pattern)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for repo in expanded:
        if repo not in seen:
            seen.add(repo)
            result.append(repo)

    return result


def _omni_search_code(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-search across multiple repositories.

    Called when repository_alias is an array of repository names.
    Aggregates results from all specified repos, sorted by score.

    Story #36: Refactored to use MultiSearchService.search() for parallel execution
    instead of inline asyncio.gather implementation.

    Story #51: Converted from async to sync for FastAPI thread pool execution.
    """
    from collections import defaultdict
    from ..multi.multi_search_config import MultiSearchConfig
    from ..multi.multi_search_service import MultiSearchService
    from ..multi.models import MultiSearchRequest

    repo_aliases = params.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases)
    limit = _coerce_int(params.get("limit"), 10)

    # Smart context-aware defaults: multi-repo (2+) uses per_repo, single repo uses global
    if len(repo_aliases) > 1:
        aggregation_mode = params.get("aggregation_mode", "per_repo")
    else:
        aggregation_mode = params.get("aggregation_mode", "global")

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "results": {
                    "cursor": "",
                    "total_results": 0,
                    "total_repos_searched": 0,
                    "results": [],
                    "errors": {},
                },
            }
        )

    # Story #36: Get config from ConfigService for MultiSearchService
    # Use MultiSearchConfig.from_config() to ensure MCP and REST use unified settings:
    # - multi_search_max_workers (unified)
    # - multi_search_timeout_seconds (unified)
    # NOT the MCP-specific omni_max_workers/omni_per_repo_timeout_seconds.
    config_service = get_config_service()
    config = MultiSearchConfig.from_config(config_service)

    # Story #36: Map MCP search_mode to MultiSearchRequest search_type
    search_mode = params.get("search_mode", "semantic")
    search_type = (
        search_mode if search_mode in ["semantic", "fts", "regex"] else "semantic"
    )
    # Handle temporal queries - map to temporal search_type
    if _is_temporal_query(params):
        search_type = "temporal"

    # Track API metrics for multi-repo searches
    # (Single-repo searches are tracked in semantic_query_manager._perform_search)
    if search_type == "semantic":
        api_metrics_service.increment_semantic_search()
    elif search_type == "regex":
        api_metrics_service.increment_regex_search()
    else:
        # FTS, temporal, hybrid all go to other_index_searches bucket
        api_metrics_service.increment_other_index_search()

    # Story #36: Create MultiSearchRequest from MCP params
    request = MultiSearchRequest(
        repositories=repo_aliases,
        query=params.get("query_text", ""),
        search_type=search_type,
        limit=limit,
        min_score=_coerce_float(params.get("min_score"), 0.0) if params.get("min_score") is not None else None,
        language=params.get("language"),
        path_filter=params.get("path_filter"),
    )

    # Story #36: Delegate to MultiSearchService for parallel execution
    # Story #51: service.search() is now synchronous
    service = MultiSearchService(config)
    try:
        response = service.search(request)
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-031",
                f"MultiSearchService failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": True,
                "results": {
                    "cursor": "",
                    "total_results": 0,
                    "total_repos_searched": 0,
                    "results": [],
                    "errors": {"service_error": str(e)},
                },
            }
        )

    # Story #182: Load category map for result enrichment
    category_map = {}
    try:
        if hasattr(app_module, "golden_repo_manager") and app_module.golden_repo_manager:
            category_service = getattr(
                app_module.golden_repo_manager, "_repo_category_service", None
            )
            if category_service:
                category_map = category_service.get_repo_category_map()
    except Exception as e:
        # Log but don't fail if category lookup fails
        logger.warning(
            format_error_log(
                "MCP-GENERAL-036",
                f"Failed to load category map in _omni_search_code: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )

    # Story #292: Build wiki-enabled repos set once per request (AC2)
    wiki_enabled_repos = _get_wiki_enabled_repos()

    # Story #36: Convert MultiSearchResponse (grouped by repo) to flat list with source_repo
    all_results = []
    for repo_alias, repo_results in response.results.items():
        for result in repo_results:
            result["source_repo"] = repo_alias
            # Normalize score field name for consistency
            if "score" in result and "similarity_score" not in result:
                result["similarity_score"] = result["score"]

            # Story #182: Enrich with category info
            # Strip -global suffix to get golden repo alias
            golden_alias = repo_alias.replace("-global", "") if repo_alias else None
            if golden_alias:
                category_info = category_map.get(golden_alias, {})
                result["repo_category"] = category_info.get("category_name")

            # Story #292: Enrich with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
            _enrich_with_wiki_url(
                result,
                result.get("file_path", ""),
                repo_alias,
                wiki_enabled_repos,
            )

            all_results.append(result)

    errors = response.errors or {}
    repos_searched = response.metadata.total_repos_searched

    # Aggregate results based on mode
    if aggregation_mode == "per_repo":
        # Per-repo mode: take proportional results from each repo
        results_by_repo = defaultdict(list)
        for r in all_results:
            results_by_repo[r.get("source_repo", "unknown")].append(r)

        # Sort each repo's results by score
        for repo in results_by_repo:
            results_by_repo[repo].sort(
                key=lambda x: x.get("similarity_score", x.get("score", 0)), reverse=True
            )

        # Take proportional results from each repo
        num_repos = len(results_by_repo)
        if num_repos > 0:
            per_repo_limit = limit // num_repos
            remainder = limit % num_repos
            final_results = []
            for i, (repo, results) in enumerate(results_by_repo.items()):
                # Give first 'remainder' repos one extra result
                repo_limit = per_repo_limit + (1 if i < remainder else 0)
                final_results.extend(results[:repo_limit])
        else:
            final_results = []
    else:
        # Global mode: sort all by score, take top N
        all_results.sort(
            key=lambda x: x.get("similarity_score", x.get("score", 0)), reverse=True
        )
        final_results = all_results[:limit]

    # Smart context-aware defaults: multi-repo (2+) uses grouped, single repo uses flat
    if len(repo_aliases) > 1:
        response_format = params.get("response_format", "grouped")
    else:
        response_format = params.get("response_format", "flat")

    # Story #683: Apply payload truncation to aggregated multi-repo results
    # This ensures consistency with REST API which calls _apply_multi_truncation()
    # Story #50: Truncation functions are now sync
    if final_results:
        if search_mode in ["fts", "hybrid"]:
            final_results = _apply_fts_payload_truncation(final_results)
        elif _is_temporal_query(params):
            final_results = _apply_temporal_payload_truncation(final_results)
        else:
            final_results = _apply_payload_truncation(final_results)

    # Use _format_omni_response helper to format results
    formatted = _format_omni_response(
        all_results=final_results,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
        cursor="",
    )

    # Add temporal_status if this is a temporal query (Story #583)
    if _is_temporal_query(params):
        temporal_status = _get_temporal_status(repo_aliases)
        if temporal_status:
            formatted["temporal_status"] = temporal_status

    # Wrap in nested "results" key for backward compatibility with existing API contract
    return _mcp_response(
        {
            "success": True,
            "results": formatted,
        }
    )


def search_code(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Search code using semantic search, FTS, or hybrid mode."""
    try:
        from pathlib import Path

        # Story #4 AC2: Metrics tracking moved to service layer
        # SemanticQueryManager._perform_search() now handles metrics
        # This ensures both MCP and REST API calls are counted

        repository_alias = params.get("repository_alias")

        # Handle JSON string arrays (from MCP clients that serialize arrays as strings)
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias  # Update params for downstream

        # Route to omni-search when repository_alias is an array
        # Story #51: _omni_search_code is now synchronous
        if isinstance(repository_alias, list):
            return _omni_search_code(params, user)

        # Check if this is a global repository query (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Global repository: query directly without activation requirement
            golden_repos_dir = _get_golden_repos_dir()

            # Look up global repo in GlobalRegistry to get actual path
            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["results"] = []
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["results"] = []
                return _mcp_response(error_envelope)

            global_repo_path = Path(target_path)

            # Verify global repo exists
            if not global_repo_path.exists():
                raise FileNotFoundError(
                    f"Global repository '{repository_alias}' not found at {global_repo_path}"
                )

            # Build mock repository list for _perform_search (single global repo)
            mock_user_repos = [
                {
                    "user_alias": repository_alias,
                    "repo_path": str(global_repo_path),
                    "actual_repo_id": repo_entry["repo_name"],
                }
            ]

            # Call _perform_search directly with all query parameters
            # Track query execution with QueryTracker for concurrency safety
            import time

            query_tracker = _get_query_tracker()
            index_path = target_path  # Use resolved path for tracking

            start_time = time.time()
            try:
                # Increment ref count before query (if QueryTracker available)
                if query_tracker is not None:
                    query_tracker.increment_ref(index_path)

                # Coerce numeric parameters from MCP string types (MCP protocol sends all values as strings)
                _evolution_limit_raw = params.get("evolution_limit")
                results = app_module.semantic_query_manager._perform_search(
                    username=user.username,
                    user_repos=mock_user_repos,
                    query_text=params["query_text"],
                    limit=_coerce_int(params.get("limit"), 10),
                    min_score=_coerce_float(params.get("min_score"), 0.5),
                    file_extensions=params.get("file_extensions"),
                    language=params.get("language"),
                    exclude_language=params.get("exclude_language"),
                    path_filter=params.get("path_filter"),
                    exclude_path=params.get("exclude_path"),
                    accuracy=params.get("accuracy", "balanced"),
                    # Search mode (Story #503 - FTS Bug Fix)
                    search_mode=params.get("search_mode", "semantic"),
                    # Temporal query parameters (Story #446)
                    time_range=params.get("time_range"),
                    time_range_all=params.get("time_range_all", False),
                    at_commit=params.get("at_commit"),
                    include_removed=params.get("include_removed", False),
                    show_evolution=params.get("show_evolution", False),
                    evolution_limit=_coerce_int(_evolution_limit_raw, 0) if _evolution_limit_raw is not None else None,
                    # FTS-specific parameters (Story #503 Phase 2)
                    case_sensitive=params.get("case_sensitive", False),
                    fuzzy=params.get("fuzzy", False),
                    edit_distance=_coerce_int(params.get("edit_distance"), 0),
                    snippet_lines=_coerce_int(params.get("snippet_lines"), 5),
                    regex=params.get("regex", False),
                    # Temporal filtering parameters (Story #503 Phase 3)
                    diff_type=params.get("diff_type"),
                    author=params.get("author"),
                    chunk_type=params.get("chunk_type"),
                )
                execution_time_ms = int((time.time() - start_time) * 1000)
                timeout_occurred = False
            except TimeoutError as e:
                execution_time_ms = int((time.time() - start_time) * 1000)
                timeout_occurred = True
                raise Exception(f"Query timed out: {str(e)}")
            except Exception as e:
                execution_time_ms = int((time.time() - start_time) * 1000)
                if "timeout" in str(e).lower():
                    raise Exception(f"Query timed out: {str(e)}")
                raise
            finally:
                # Always decrement ref count when query completes (if QueryTracker available)
                if query_tracker is not None:
                    query_tracker.decrement_ref(index_path)

            # Story #182: Load category map for result enrichment
            category_map = {}
            try:
                if hasattr(app_module, "golden_repo_manager") and app_module.golden_repo_manager:
                    category_service = getattr(
                        app_module.golden_repo_manager, "_repo_category_service", None
                    )
                    if category_service:
                        category_map = category_service.get_repo_category_map()
            except Exception as e:
                # Log but don't fail if category lookup fails
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-037",
                        f"Failed to load category map in search_code: {e}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )

            # Story #292: Build wiki-enabled repos set once per request (AC2)
            wiki_enabled_repos = _get_wiki_enabled_repos()

            # Build response matching query_user_repositories format
            response_results = []
            for r in results:
                result_dict = r.to_dict()
                result_dict["source_repo"] = (
                    repository_alias  # Fix: Set source_repo for single-repo searches
                )

                # Story #182: Enrich with category info
                # Strip -global suffix to get golden repo alias
                golden_alias = repository_alias.replace("-global", "") if repository_alias else None
                if golden_alias:
                    category_info = category_map.get(golden_alias, {})
                    result_dict["repo_category"] = category_info.get("category_name")

                # Story #292: Enrich with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
                _enrich_with_wiki_url(
                    result_dict,
                    result_dict.get("file_path", ""),
                    repository_alias,
                    wiki_enabled_repos,
                )

                response_results.append(result_dict)

            # Apply payload truncation based on search mode
            # Story #50: Truncation functions are now sync
            search_mode = params.get("search_mode", "semantic")
            if search_mode in ["fts", "hybrid"]:
                # Story #680: FTS truncation for code_snippet and match_text
                response_results = _apply_fts_payload_truncation(response_results)
            # Story #681: Temporal truncation for temporal queries
            if _is_temporal_query(params):
                response_results = _apply_temporal_payload_truncation(response_results)
            else:
                # Story #679: Semantic truncation for content field
                response_results = _apply_payload_truncation(response_results)

            result = {
                "results": response_results,
                "total_results": len(response_results),
                "query_metadata": {
                    "query_text": params["query_text"],
                    "execution_time_ms": execution_time_ms,
                    "repositories_searched": 1,
                    "timeout_occurred": timeout_occurred,
                },
            }

            return _mcp_response({"success": True, "results": result})

        # Activated repository: use semantic_query_manager for activated repositories (matches REST endpoint pattern)
        # Coerce numeric parameters from MCP string types (MCP protocol sends all values as strings)
        _evolution_limit_activated = params.get("evolution_limit")
        result = app_module.semantic_query_manager.query_user_repositories(
            username=user.username,
            query_text=params["query_text"],
            repository_alias=params.get("repository_alias"),
            limit=_coerce_int(params.get("limit"), 10),
            min_score=_coerce_float(params.get("min_score"), 0.5),
            file_extensions=params.get("file_extensions"),
            language=params.get("language"),
            exclude_language=params.get("exclude_language"),
            path_filter=params.get("path_filter"),
            exclude_path=params.get("exclude_path"),
            accuracy=params.get("accuracy", "balanced"),
            # Search mode (Story #503 - FTS Bug Fix)
            search_mode=params.get("search_mode", "semantic"),
            # Temporal query parameters (Story #446)
            time_range=params.get("time_range"),
            time_range_all=params.get("time_range_all", False),
            at_commit=params.get("at_commit"),
            include_removed=params.get("include_removed", False),
            show_evolution=params.get("show_evolution", False),
            evolution_limit=_coerce_int(_evolution_limit_activated, 0) if _evolution_limit_activated is not None else None,
            # FTS-specific parameters (Story #503 Phase 2)
            case_sensitive=params.get("case_sensitive", False),
            fuzzy=params.get("fuzzy", False),
            edit_distance=_coerce_int(params.get("edit_distance"), 0),
            snippet_lines=_coerce_int(params.get("snippet_lines"), 5),
            regex=params.get("regex", False),
            # Temporal filtering parameters (Story #503 Phase 3)
            diff_type=params.get("diff_type"),
            author=params.get("author"),
            chunk_type=params.get("chunk_type"),
        )

        # Story #182: Load category map for result enrichment (activated repos)
        category_map = {}
        try:
            if hasattr(app_module, "golden_repo_manager") and app_module.golden_repo_manager:
                category_service = getattr(
                    app_module.golden_repo_manager, "_repo_category_service", None
                )
                if category_service:
                    category_map = category_service.get_repo_category_map()
        except Exception as e:
            # Log but don't fail if category lookup fails
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-038",
                    f"Failed to load category map in search_code (activated): {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Story #182: Enrich results with category info
        if "results" in result and isinstance(result["results"], list):
            for res in result["results"]:
                # Get the repository alias from the result (may have -global suffix)
                repo_alias = res.get("source_repo") or res.get("repository_alias")
                if repo_alias:
                    # Strip -global suffix to get golden repo alias
                    golden_alias = repo_alias.replace("-global", "")
                    category_info = category_map.get(golden_alias, {})
                    res["repo_category"] = category_info.get("category_name")

        # Apply payload truncation based on search mode
        # Story #50: Truncation functions are now sync
        if "results" in result and isinstance(result["results"], list):
            search_mode = params.get("search_mode", "semantic")
            if search_mode in ["fts", "hybrid"]:
                # Story #680: FTS truncation for code_snippet and match_text
                result["results"] = _apply_fts_payload_truncation(result["results"])
            # Story #681: Temporal truncation for temporal queries
            if _is_temporal_query(params):
                result["results"] = _apply_temporal_payload_truncation(
                    result["results"]
                )
            else:
                # Story #679: Semantic truncation for content field
                result["results"] = _apply_payload_truncation(result["results"])

        return _mcp_response({"success": True, "results": result})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "results": []})


def discover_repositories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Discover available repositories from configured sources."""
    try:
        # List all golden repositories (source_type filter not currently used)
        repos = app_module.golden_repo_manager.list_golden_repos()

        return _mcp_response({"success": True, "repositories": repos})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "repositories": []})


def list_repositories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List activated repositories for the current user, plus global repos."""
    try:
        # Story #196: Whitelist of MCP-relevant fields for activated repos
        ACTIVATED_REPO_FIELDS = {
            "user_alias",
            "golden_repo_alias",
            "current_branch",
            "is_global",
            "repo_url",
            "last_refresh",
            "repo_category",
            "is_composite",
            "golden_repo_aliases",  # For composite repos only
        }

        # Get activated repos from database
        raw_activated_repos = app_module.activated_repo_manager.list_activated_repositories(
            user.username
        )

        # Story #196: Filter activated repos to only include whitelisted fields
        activated_repos = []
        for repo in raw_activated_repos:
            filtered_repo = {k: v for k, v in repo.items() if k in ACTIVATED_REPO_FIELDS}
            activated_repos.append(filtered_repo)

        # Get global repos from GlobalRegistry
        global_repos = []
        try:
            golden_repos_dir = _get_golden_repos_dir()
            registry = get_server_global_registry(golden_repos_dir)
            global_repos_data = registry.list_global_repos()

            # Normalize global repos schema to match activated repos
            for repo in global_repos_data:
                # Validate required fields exist
                if "alias_name" not in repo or "repo_name" not in repo:
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-032",
                            f"Skipping malformed global repo entry: {repo}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    continue

                normalized = {
                    "user_alias": repo["alias_name"],  # Map alias_name → user_alias
                    "golden_repo_alias": repo[
                        "repo_name"
                    ],  # Map repo_name → golden_repo_alias
                    "current_branch": None,  # Global repos are read-only snapshots
                    "is_global": True,
                    "repo_url": repo.get("repo_url"),
                    "last_refresh": repo.get("last_refresh"),
                    # Story #196: Removed index_path and created_at (internal fields, not MCP-relevant)
                }
                global_repos.append(normalized)

        except Exception as e:
            # Log but don't fail - continue with activated repos only
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-033",
                    f"Failed to load global repos from {golden_repos_dir}: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Merge activated and global repos
        all_repos = activated_repos + global_repos

        # Story #182: Enrich repos with category information
        category_map = {}
        try:
            if hasattr(app_module, "golden_repo_manager") and app_module.golden_repo_manager:
                category_service = getattr(
                    app_module.golden_repo_manager, "_repo_category_service", None
                )
                if category_service:
                    category_map = category_service.get_repo_category_map()
        except Exception as e:
            # Log but don't fail if category lookup fails
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-034",
                    f"Failed to load category map: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Enrich each repo with category information
        for repo in all_repos:
            # For activated repos, use golden_repo_alias to look up category
            # For global repos, use golden_repo_alias (same field)
            golden_alias = repo.get("golden_repo_alias")
            category_info = category_map.get(golden_alias, {})
            repo["repo_category"] = category_info.get("category_name")

        # Story #182: Filter by category if requested
        category_filter = params.get("category")
        if category_filter:
            if category_filter == "Unassigned":
                # Filter for repos with NULL category
                all_repos = [r for r in all_repos if r["repo_category"] is None]
            else:
                # Filter for repos matching the specified category name
                all_repos = [r for r in all_repos if r["repo_category"] == category_filter]

        # Story #182: Sort by category priority (ascending), then Unassigned last, then alphabetically
        def sort_key(repo):
            golden_alias = repo.get("golden_repo_alias")
            category_info = category_map.get(golden_alias, {})
            priority = category_info.get("priority")

            # Repos with priority come first (sorted by priority),
            # then Unassigned repos (priority=None) at the end
            if priority is None:
                # Use large number to sort Unassigned last
                return (float('inf'), repo.get("user_alias", ""))
            else:
                return (priority, repo.get("user_alias", ""))

        all_repos.sort(key=sort_key)

        return _mcp_response({"success": True, "repositories": all_repos})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "repositories": []})


def activate_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Activate a repository for querying (supports single or composite)."""
    try:
        job_id = app_module.activated_repo_manager.activate_repository(
            username=user.username,
            golden_repo_alias=params.get("golden_repo_alias"),
            golden_repo_aliases=params.get("golden_repo_aliases"),
            branch_name=params.get("branch_name"),
            user_alias=params.get("user_alias"),
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": "Repository activation started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def deactivate_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Deactivate a repository."""
    try:
        user_alias = params["user_alias"]
        job_id = app_module.activated_repo_manager.deactivate_repository(
            username=user.username, user_alias=user_alias
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Repository '{user_alias}' deactivation started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def list_repo_categories(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all repository categories (Story #182)."""
    try:
        # Get category service from golden_repo_manager
        if not hasattr(app_module, "golden_repo_manager") or not app_module.golden_repo_manager:
            return _mcp_response({
                "success": False,
                "error": "Category service not available",
                "categories": [],
                "total": 0
            })

        category_service = getattr(
            app_module.golden_repo_manager, "_repo_category_service", None
        )
        if not category_service:
            return _mcp_response({
                "success": False,
                "error": "Category service not initialized",
                "categories": [],
                "total": 0
            })

        # Get all categories
        categories = category_service.list_categories()

        return _mcp_response({
            "success": True,
            "categories": categories,
            "total": len(categories)
        })
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-035",
                f"Failed to list repository categories: {e}",
                exc_info=True,
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({
            "success": False,
            "error": str(e),
            "categories": [],
            "total": 0
        })


def get_repository_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get detailed status of a repository."""

    try:
        user_alias = params["repository_alias"]

        # Load category map for enrichment (Story #182 pattern)
        category_map = {}
        try:
            if hasattr(app_module, "golden_repo_manager") and app_module.golden_repo_manager:
                category_service = getattr(
                    app_module.golden_repo_manager, "_repo_category_service", None
                )
                if category_service:
                    category_map = category_service.get_repo_category_map()
        except Exception as e:
            # Log but don't fail if category lookup fails
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-035",
                    f"Failed to load category map in get_repository_status: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Check if this is a global repository (ends with -global suffix)
        if user_alias and user_alias.endswith("-global"):
            golden_repos_dir = _get_golden_repos_dir()
            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()

            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == user_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{user_alias}' not found",
                    attempted_value=user_alias,
                    available_values=available_repos,
                )
                error_envelope["status"] = {}
                return _mcp_response(error_envelope)

            # Build status directly from registry entry (no alias file needed)
            status = {
                "user_alias": repo_entry["alias_name"],
                "golden_repo_alias": repo_entry.get("repo_name"),
                "repo_url": repo_entry.get("repo_url"),
                "is_global": True,
                "path": repo_entry.get("index_path"),
                "last_refresh": repo_entry.get("last_refresh"),
                "created_at": repo_entry.get("created_at"),
                "index_path": repo_entry.get("index_path"),
            }

            # Enrich with category info (Story #182)
            golden_alias = repo_entry.get("repo_name")
            category_info = category_map.get(golden_alias, {})
            status["repo_category"] = category_info.get("category_name")

            return _mcp_response({"success": True, "status": status})

        # Activated repository (original code)
        status = app_module.repository_listing_manager.get_repository_details(
            user_alias, user.username
        )

        # Enrich with category info (Story #182)
        golden_alias = status.get("golden_repo_alias")
        if golden_alias:
            category_info = category_map.get(golden_alias, {})
            status["repo_category"] = category_info.get("category_name")

        return _mcp_response({"success": True, "status": status})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "status": {}})


def sync_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Sync repository with upstream."""
    try:
        user_alias = params["user_alias"]
        # Resolve alias to repository details
        repos = app_module.activated_repo_manager.list_activated_repositories(
            user.username
        )
        repo_id = None
        for repo in repos:
            if repo["user_alias"] == user_alias:
                repo_id = repo.get("actual_repo_id", user_alias)
                break

        if not repo_id:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Repository '.*' not found",
                    "job_id": None,
                }
            )

        # Defensive check
        if app_module.background_job_manager is None:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Background job manager not initialized",
                    "job_id": None,
                }
            )

        # Create sync job wrapper function
        from code_indexer.server.app import _execute_repository_sync

        def sync_job_wrapper():
            return _execute_repository_sync(
                repo_id=repo_id,
                username=user.username,
                options={},
                progress_callback=None,
            )

        # Submit sync job with correct signature
        job_id = app_module.background_job_manager.submit_job(
            operation_type="sync_repository",
            func=sync_job_wrapper,
            submitter_username=user.username,
            repo_alias=repo_id,  # AC5: Fix unknown repo bug
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Repository '{user_alias}' sync started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def switch_branch(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Switch repository to different branch."""
    try:
        user_alias = params["user_alias"]
        branch_name = params["branch_name"]
        create = params.get("create", False)

        # Use activated_repo_manager.switch_branch (matches app.py endpoint pattern)
        result = app_module.activated_repo_manager.switch_branch(
            username=user.username,
            user_alias=user_alias,
            branch_name=branch_name,
            create=create,
        )
        return _mcp_response({"success": True, "message": result["message"]})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})


def _omni_list_files(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-list-files across multiple repositories."""
    import json as json_module

    repo_aliases = params.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "files": [],
                "total_files": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    all_files = []
    errors = {}
    repos_searched = 0

    for repo_alias in repo_aliases:
        try:
            single_params = dict(params)
            single_params["repository_alias"] = repo_alias

            single_result = list_files(single_params, user)

            content = single_result.get("content", [])
            if content and content[0].get("type") == "text":
                result_data = json_module.loads(content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    files_list = result_data.get("files", [])
                    for f in files_list:
                        f["source_repo"] = repo_alias
                    all_files.extend(files_list)
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-034",
                    f"Omni-list-files failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    # Get response_format parameter (default to "flat" for backward compatibility)
    response_format = params.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_files,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    # Add files-specific field for backward compatibility
    if response_format == "flat":
        formatted["files"] = formatted.pop("results")
        formatted["total_files"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def list_files(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List files in a repository."""
    from code_indexer.server.models.api_models import FileListQueryParams
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]
        repository_alias = _parse_json_string_array(repository_alias)
        params["repository_alias"] = repository_alias  # Update params for downstream

        # Route to omni-search when repository_alias is an array
        if isinstance(repository_alias, list):
            return _omni_list_files(params, user)

        # Extract parameters for path pattern building
        path = params.get("path", "")
        recursive = params.get(
            "recursive", True
        )  # Default to recursive for backward compatibility
        user_path_pattern = params.get("path_pattern")  # Optional advanced filtering

        # Build path pattern combining path and user's pattern
        # This logic mirrors browse_directory (lines 1220-1238)
        final_path_pattern = None
        # Normalize path first (remove trailing slash) - "/" becomes ""
        path = path.rstrip("/") if path else ""
        if path:
            # Base pattern for the specified directory
            base_pattern = f"{path}/**/*" if recursive else f"{path}/*"
            if user_path_pattern:
                # Combine path with user's pattern
                # e.g., path="src", path_pattern="*.py" -> "src/**/*.py"
                if recursive:
                    final_path_pattern = f"{path}/**/{user_path_pattern}"
                else:
                    final_path_pattern = f"{path}/{user_path_pattern}"
            else:
                final_path_pattern = base_pattern
        elif user_path_pattern:
            # Just use the user's pattern directly
            final_path_pattern = user_path_pattern
        # else: final_path_pattern stays None (all files)

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["files"] = []
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["files"] = []
                return _mcp_response(error_envelope)

            # Use resolved path instead of alias for file_service
            query_params = FileListQueryParams(
                page=1,
                limit=500,  # Max limit for MCP tool usage
                path_pattern=final_path_pattern,
            )

            result = app_module.file_service.list_files_by_path(
                repo_path=target_path,
                query_params=query_params,
            )
        else:
            # Create FileListQueryParams object as required by service method signature
            query_params = FileListQueryParams(
                page=1,
                limit=500,  # Max limit for MCP tool usage
                path_pattern=final_path_pattern,
            )

            # Call with correct signature: list_files(repo_id, username, query_params)
            result = app_module.file_service.list_files(
                repo_id=repository_alias,
                username=user.username,
                query_params=query_params,
            )

        # Extract files from FileListResponse and serialize FileInfo objects
        # Handle both FileListResponse objects and plain dicts
        if hasattr(result, "files"):
            # FileListResponse object with FileInfo objects
            files_data = result.files
        elif isinstance(result, dict):
            # Plain dict (for backward compatibility with tests)
            files_data = result.get("files", [])
        else:
            files_data = []

        # Convert FileInfo Pydantic objects to dicts with proper datetime serialization
        # Use mode='json' to convert datetime objects to ISO format strings
        serialized_files = [
            f.model_dump(mode="json") if hasattr(f, "model_dump") else f
            for f in files_data
        ]

        return _mcp_response({"success": True, "files": serialized_files})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "files": []})


def get_file_content(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get content of a specific file with optional pagination.

    Returns MCP-compliant response with content as array of text blocks.
    Per MCP spec, content must be an array of content blocks, each with 'type' and 'text' fields.

    Pagination parameters:
    - offset: 1-indexed line number to start reading from (optional, default: 1)
    - limit: Maximum number of lines to return (optional, default: None = all lines)
    """
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]
        file_path = params["file_path"]

        # Extract optional pagination parameters
        # Coerce from MCP string types (MCP protocol sends all values as strings)
        # Non-integer floats (e.g. 3.14) are treated as invalid (coerced to 0, fails >= 1 check)
        _offset_raw = params.get("offset")
        _limit_raw = params.get("limit")
        _offset_invalid = isinstance(_offset_raw, float) and not float(_offset_raw).is_integer()
        _limit_invalid = isinstance(_limit_raw, float) and not float(_limit_raw).is_integer()
        offset = 0 if _offset_invalid else (_coerce_int(_offset_raw, 0) if _offset_raw is not None else None)
        limit = 0 if _limit_invalid else (_coerce_int(_limit_raw, 0) if _limit_raw is not None else None)

        # Validate offset if provided
        if offset is not None:
            if offset < 1:
                return _mcp_response(
                    {
                        "success": False,
                        "error": "offset must be an integer >= 1",
                        "content": [],
                        "metadata": {},
                    }
                )

        # Validate limit if provided
        if limit is not None:
            if limit < 1:
                return _mcp_response(
                    {
                        "success": False,
                        "error": "limit must be an integer >= 1",
                        "content": [],
                        "metadata": {},
                    }
                )

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["content"] = []
                error_envelope["metadata"] = {}
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["content"] = []
                error_envelope["metadata"] = {}
                return _mcp_response(error_envelope)

            # Use resolved path for file_service with pagination parameters
            # Story #33 Fix: Use skip_truncation=True so TruncationHelper handles
            # truncation with cache_handle support (avoids double truncation)
            result = app_module.file_service.get_file_content_by_path(
                repo_path=target_path,
                file_path=file_path,
                offset=offset,
                limit=limit,
                skip_truncation=True,
            )
        else:
            # Call file_service with pagination parameters
            # Story #33 Fix: Use skip_truncation=True so TruncationHelper handles
            # truncation with cache_handle support (avoids double truncation)
            result = app_module.file_service.get_file_content(
                repository_alias=repository_alias,
                file_path=file_path,
                username=user.username,
                offset=offset,
                limit=limit,
                skip_truncation=True,
            )

        # Story #33: Apply token-based truncation with cache handle support
        file_content = result.get("content", "")
        metadata = result.get("metadata", {})

        # Get payload cache and content limits config
        payload_cache = getattr(app_module.app.state, "payload_cache", None)
        config_service = get_config_service()
        content_limits = config_service.get_config().content_limits_config

        # Apply truncation if cache is available
        cache_handle = None
        truncated = False
        total_tokens = 0
        preview_tokens = 0
        total_pages = 0
        has_more = False

        if payload_cache is not None and file_content and content_limits is not None:
            from code_indexer.server.cache.truncation_helper import TruncationHelper

            truncation_helper = TruncationHelper(payload_cache, content_limits)
            truncation_result = truncation_helper.truncate_and_cache(
                content=file_content,
                content_type="file",
            )

            file_content = truncation_result.preview
            cache_handle = truncation_result.cache_handle
            truncated = truncation_result.truncated
            total_tokens = truncation_result.original_tokens
            preview_tokens = truncation_result.preview_tokens
            total_pages = truncation_result.total_pages
            has_more = truncation_result.has_more

        # MCP spec: content must be array of content blocks
        content_blocks = (
            [{"type": "text", "text": file_content}] if file_content else []
        )

        # Story #33: Add truncation fields to metadata for backward compatibility.
        # Note: cache_handle, truncated, total_pages, has_more appear in BOTH metadata
        # and top-level response.
        # - Metadata location: For clients that parse nested metadata object
        # - Top-level location: For clients that expect flat response structure
        # This duplication ensures backward compatibility with existing clients (AC4).
        metadata["cache_handle"] = cache_handle
        metadata["truncated"] = truncated
        metadata["total_tokens"] = total_tokens
        metadata["preview_tokens"] = preview_tokens
        metadata["total_pages"] = total_pages
        metadata["has_more"] = has_more

        # Story #292: Enrich metadata with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
        wiki_enabled_repos = _get_wiki_enabled_repos()
        _enrich_with_wiki_url(metadata, file_path, repository_alias, wiki_enabled_repos)

        return _mcp_response(
            {
                "success": True,
                "content": content_blocks,
                "metadata": metadata,
                # Duplicate at top level for flat response structure clients
                "cache_handle": cache_handle,
                "truncated": truncated,
                "total_pages": total_pages,
                "has_more": has_more,
            }
        )
    except Exception as e:
        # Even on error, content must be an array (empty array is valid)
        return _mcp_response(
            {"success": False, "error": str(e), "content": [], "metadata": {}}
        )


def browse_directory(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Browse directory recursively.

    FileListingService doesn't have browse_directory method.
    Use list_files with path patterns instead.
    """
    from code_indexer.server.models.api_models import FileListQueryParams
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]
        path = params.get("path", "")
        recursive = params.get("recursive", True)
        user_path_pattern = params.get("path_pattern")
        language = params.get("language")
        limit = _coerce_int(params.get("limit"), 500)
        sort_by = params.get("sort_by", "path")

        # Validate limit range
        if limit < 1:
            limit = 1
        elif limit > 500:
            limit = 500

        # Validate sort_by value
        if sort_by not in ("path", "size", "modified_at"):
            sort_by = "path"

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["structure"] = {}
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["structure"] = {}
                return _mcp_response(error_envelope)

            # Use resolved path instead of alias for file_service
            repository_alias = target_path
            is_global_repo = True
        else:
            is_global_repo = False

        # Build path pattern combining path and user's pattern
        final_path_pattern = None
        # Normalize path first (remove trailing slash) - "/" becomes ""
        path = path.rstrip("/") if path else ""

        # Determine if user_path_pattern is absolute or relative
        # Absolute patterns contain '/' or '**' (e.g., "code/src/**/*.java", "**/*.py", "src/main/*.py")
        # Relative patterns are simple globs (e.g., "*.py", "*.{py,java}")
        is_absolute_pattern = False
        if user_path_pattern:
            is_absolute_pattern = (
                "/" in user_path_pattern or user_path_pattern.startswith("**")
            )

        if path:
            # Base pattern for the specified directory
            base_pattern = f"{path}/**/*" if recursive else f"{path}/*"
            if user_path_pattern:
                if is_absolute_pattern:
                    # Absolute pattern: use it directly, ignore path parameter
                    # e.g., path="wrong/path", path_pattern="code/src/**/*.java" -> "code/src/**/*.java"
                    final_path_pattern = user_path_pattern
                else:
                    # Relative pattern: combine path with user's pattern
                    # e.g., path="src", path_pattern="*.py" -> "src/**/*.py"
                    if recursive:
                        final_path_pattern = f"{path}/**/{user_path_pattern}"
                    else:
                        final_path_pattern = f"{path}/{user_path_pattern}"
            else:
                final_path_pattern = base_pattern
        elif user_path_pattern:
            # Just use the user's pattern directly
            final_path_pattern = user_path_pattern
        # else: final_path_pattern stays None (all files)

        # Use list_files with user-specified limit
        query_params = FileListQueryParams(
            page=1,
            limit=limit,
            path_pattern=final_path_pattern,
            language=language,
            sort_by=sort_by,
        )

        if is_global_repo:
            result = app_module.file_service.list_files_by_path(
                repo_path=repository_alias,
                query_params=query_params,
            )
        else:
            result = app_module.file_service.list_files(
                repo_id=repository_alias,
                username=user.username,
                query_params=query_params,
            )

        # Convert FileInfo objects to dict structure
        files_data = (
            result.files if hasattr(result, "files") else result.get("files", [])
        )
        serialized_files = [
            f.model_dump(mode="json") if hasattr(f, "model_dump") else f
            for f in files_data
        ]

        # Build directory structure from file list
        structure = {
            "path": path or "/",
            "files": serialized_files,
            "total": len(serialized_files),
        }

        return _mcp_response({"success": True, "structure": structure})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "structure": {}})


def get_branches(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get available branches for a repository."""
    from pathlib import Path
    from code_indexer.services.git_topology_service import GitTopologyService
    from code_indexer.server.services.branch_service import BranchService

    try:
        repository_alias = params["repository_alias"]
        include_remote = params.get("include_remote", False)

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            # Look up global repo in GlobalRegistry to get actual path
            golden_repos_dir = _get_golden_repos_dir()

            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()

            # Find the matching global repo
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["branches"] = []
                return _mcp_response(error_envelope)

            # Use AliasManager to get current target path (registry path becomes stale after refresh)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["branches"] = []
                return _mcp_response(error_envelope)

            # Use resolved path for git operations
            repo_path = target_path
        else:
            # Get repository path (matches app.py endpoint pattern at line 4383-4395)
            repo_path = app_module.activated_repo_manager.get_activated_repo_path(
                username=user.username,
                user_alias=repository_alias,
            )

        # Initialize git topology service
        git_topology_service = GitTopologyService(Path(repo_path))

        # Use BranchService as context manager (matches app.py pattern at line 4404-4408)
        with BranchService(
            git_topology_service=git_topology_service, index_status_manager=None
        ) as branch_service:
            # Get branch information
            branches = branch_service.list_branches(include_remote=include_remote)

            # Convert BranchInfo objects to dicts for JSON serialization
            branches_data = [
                {
                    "name": b.name,
                    "is_current": b.is_current,
                    "last_commit": {
                        "sha": b.last_commit.sha,
                        "message": b.last_commit.message,
                        "author": b.last_commit.author,
                        "date": b.last_commit.date,
                    },
                    "index_status": (
                        {
                            "status": b.index_status.status,
                            "files_indexed": b.index_status.files_indexed,
                            "total_files": b.index_status.total_files,
                            "last_indexed": b.index_status.last_indexed,
                            "progress_percentage": b.index_status.progress_percentage,
                        }
                        if b.index_status
                        else None
                    ),
                    "remote_tracking": (
                        {
                            "remote": b.remote_tracking.remote,
                            "ahead": b.remote_tracking.ahead,
                            "behind": b.remote_tracking.behind,
                        }
                        if b.remote_tracking
                        else None
                    ),
                }
                for b in branches
            ]

            return _mcp_response({"success": True, "branches": branches_data})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "branches": []})


def check_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Check system health status."""
    try:
        from code_indexer import __version__
        from code_indexer.server.services.health_service import health_service

        # Call the actual method (not async)
        health_response = health_service.get_system_health()
        # Use mode='json' to serialize datetime objects to ISO format strings
        return _mcp_response(
            {
                "success": True,
                "server_version": __version__,
                "health": health_response.model_dump(mode="json"),
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "health": {}})


def check_hnsw_health(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Check HNSW index health and integrity for a repository.

    Performs comprehensive health check on the repository's HNSW index including:
    - File existence and readability
    - HNSW loadability
    - Integrity validation (connections, inbound links)
    - File metadata (size, modification time)

    Results are cached for 5 minutes unless force_refresh=True.
    """
    try:
        from pathlib import Path

        repository_alias = params.get("repository_alias")
        force_refresh = params.get("force_refresh", False)

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )

        # Resolve repository alias to clone path
        repo = app_module.golden_repo_manager.get_golden_repo(repository_alias)
        if not repo:
            return _mcp_response(
                {"success": False, "error": f"Repository not found: {repository_alias}"}
            )

        # Construct index path (assumes default collection name)
        clone_path = Path(repo.clone_path)
        index_path = clone_path / ".code-indexer" / "index" / "default" / "index.bin"

        # Get singleton service instance (cache persists across requests)
        health_service = _get_hnsw_health_service()

        # Perform health check
        result = health_service.check_health(
            index_path=str(index_path),
            force_refresh=force_refresh,
        )

        # Use mode='json' to serialize datetime objects to ISO format strings
        return _mcp_response(
            {
                "success": True,
                "health": result.model_dump(mode="json"),
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in check_hnsw_health: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def add_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Add a golden repository (admin only).

    Supports temporal indexing via enable_temporal and temporal_options parameters.
    When enable_temporal=True, the repository will be indexed with --index-commits
    to support time-based searches (git history search).
    """
    try:
        repo_url = params["url"]
        alias = params["alias"]
        default_branch = params.get("branch", "main")

        # Extract temporal indexing parameters (Story #527)
        enable_temporal = params.get("enable_temporal", False)
        temporal_options = params.get("temporal_options")

        job_id = app_module.golden_repo_manager.add_golden_repo(
            repo_url=repo_url,
            alias=alias,
            default_branch=default_branch,
            enable_temporal=enable_temporal,
            temporal_options=temporal_options,
            submitter_username=user.username,
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' addition started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})


def remove_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Remove a golden repository (admin only)."""
    try:
        alias = params["alias"]
        job_id = app_module.golden_repo_manager.remove_golden_repo(
            alias, submitter_username=user.username
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' removal started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})


def refresh_golden_repo(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Refresh a golden repository (admin only)."""
    try:
        alias = params["alias"]
        # Validate repo exists via golden_repo_manager before scheduling
        if alias not in app_module.golden_repo_manager.golden_repos:
            raise Exception(f"Golden repository '{alias}' not found")
        # Delegate to RefreshScheduler (index-source-first versioned pipeline)
        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            raise Exception("RefreshScheduler not available")
        # Resolution from bare alias to global format happens inside RefreshScheduler
        job_id = refresh_scheduler.trigger_refresh_for_repo(
            alias, submitter_username=user.username
        )
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Golden repository '{alias}' refresh started",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def list_users(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all users (admin only)."""
    try:
        all_users = app_module.user_manager.get_all_users()
        return _mcp_response(
            {
                "success": True,
                "users": [
                    {
                        "username": u.username,
                        "role": u.role.value,
                        "created_at": u.created_at.isoformat(),
                    }
                    for u in all_users
                ],
                "total": len(all_users),
            }
        )
    except Exception as e:
        return _mcp_response(
            {"success": False, "error": str(e), "users": [], "total": 0}
        )


def create_user(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new user (admin only)."""
    try:
        username = params["username"]
        password = params["password"]
        role = UserRole(params["role"])

        new_user = app_module.user_manager.create_user(
            username=username, password=password, role=role
        )
        return _mcp_response(
            {
                "success": True,
                "user": {
                    "username": new_user.username,
                    "role": new_user.role.value,
                    "created_at": new_user.created_at.isoformat(),
                },
                "message": f"User '{username}' created successfully",
            }
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "user": None})


def get_repository_statistics(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get repository statistics."""
    from pathlib import Path

    try:
        repository_alias = params["repository_alias"]

        # Check if this is a global repository (ends with -global suffix)
        if repository_alias and repository_alias.endswith("-global"):
            golden_repos_dir = _get_golden_repos_dir()
            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()

            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )

            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["statistics"] = {}
                return _mcp_response(error_envelope)

            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            target_path = alias_manager.read_alias(repository_alias)

            if not target_path:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                error_envelope["statistics"] = {}
                return _mcp_response(error_envelope)

            # Build basic statistics for global repo
            statistics = {
                "repository_alias": repository_alias,
                "is_global": True,
                "path": target_path,
                "index_path": repo_entry.get("index_path"),
            }
            return _mcp_response({"success": True, "statistics": statistics})

        # Activated repository (original code)
        from code_indexer.server.services.stats_service import stats_service

        stats_response = stats_service.get_repository_stats(
            repository_alias, username=user.username
        )
        return _mcp_response(
            {"success": True, "statistics": stats_response.model_dump(mode="json")}
        )
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "statistics": {}})


def get_job_statistics(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get background job statistics.

    BackgroundJobManager doesn't have get_job_statistics method.
    Use get_active_job_count, get_pending_job_count, get_failed_job_count instead.
    """
    try:
        active = app_module.background_job_manager.get_active_job_count()
        pending = app_module.background_job_manager.get_pending_job_count()
        failed = app_module.background_job_manager.get_failed_job_count()

        stats = {
            "active": active,
            "pending": pending,
            "failed": failed,
            "total": active + pending + failed,
        }

        return _mcp_response({"success": True, "statistics": stats})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "statistics": {}})


def get_job_details(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get detailed information about a specific job including error messages."""
    try:
        job_id = params.get("job_id")
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        job = app_module.background_job_manager.get_job_status(job_id, user.username)
        if not job:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Job '{job_id}' not found or access denied",
                }
            )

        return _mcp_response({"success": True, "job": job})
    except Exception as e:
        return _mcp_response({"success": False, "error": str(e)})


def get_all_repositories_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get status summary of all repositories."""
    try:
        # Get activated repos status
        repos = app_module.activated_repo_manager.list_activated_repositories(
            user.username
        )
        status_summary = []
        for repo in repos:
            try:
                details = app_module.repository_listing_manager.get_repository_details(
                    repo["user_alias"], user.username
                )
                status_summary.append(details)
            except Exception:
                continue

        # Get global repos status (same pattern as list_repositories handler)
        try:
            golden_repos_dir = _get_golden_repos_dir()
            registry = get_server_global_registry(golden_repos_dir)
            global_repos_data = registry.list_global_repos()

            for repo in global_repos_data:
                if "alias_name" not in repo or "repo_name" not in repo:
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-035",
                            f"Skipping malformed global repo entry: {repo}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )
                    continue

                global_status = {
                    "user_alias": repo["alias_name"],
                    "golden_repo_alias": repo["repo_name"],
                    "current_branch": None,
                    "is_global": True,
                    "repo_url": repo.get("repo_url"),
                    "last_refresh": repo.get("last_refresh"),
                    "index_path": repo.get("index_path"),
                    "created_at": repo.get("created_at"),
                }
                status_summary.append(global_status)
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-036",
                    f"Failed to load global repos status: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        return _mcp_response(
            {
                "success": True,
                "repositories": status_summary,
                "total": len(status_summary),
            }
        )
    except Exception as e:
        return _mcp_response(
            {"success": False, "error": str(e), "repositories": [], "total": 0}
        )


def manage_composite_repository(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Manage composite repository operations."""
    try:
        operation = params["operation"]
        user_alias = params["user_alias"]
        golden_repo_aliases = params.get("golden_repo_aliases", [])

        if operation == "create":
            job_id = app_module.activated_repo_manager.activate_repository(
                username=user.username,
                golden_repo_aliases=golden_repo_aliases,
                user_alias=user_alias,
            )
            return _mcp_response(
                {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Composite repository '{user_alias}' creation started",
                }
            )

        elif operation == "update":
            # For update, deactivate then reactivate
            try:
                app_module.activated_repo_manager.deactivate_repository(
                    username=user.username, user_alias=user_alias
                )
            except Exception:
                pass  # Ignore if doesn't exist

            job_id = app_module.activated_repo_manager.activate_repository(
                username=user.username,
                golden_repo_aliases=golden_repo_aliases,
                user_alias=user_alias,
            )
            return _mcp_response(
                {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Composite repository '{user_alias}' update started",
                }
            )

        elif operation == "delete":
            job_id = app_module.activated_repo_manager.deactivate_repository(
                username=user.username, user_alias=user_alias
            )
            return _mcp_response(
                {
                    "success": True,
                    "job_id": job_id,
                    "message": f"Composite repository '{user_alias}' deletion started",
                }
            )

        else:
            return _mcp_response(
                {"success": False, "error": f"Unknown operation: {operation}"}
            )

    except Exception as e:
        return _mcp_response({"success": False, "error": str(e), "job_id": None})


def handle_list_global_repos(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for list_global_repos tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_repos_dir = _get_golden_repos_dir()
    ops = GlobalRepoOperations(golden_repos_dir)
    repos = ops.list_repos()
    return _mcp_response({"success": True, "repos": repos})


def handle_global_repo_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for global_repo_status tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_repos_dir = _get_golden_repos_dir()
    ops = GlobalRepoOperations(golden_repos_dir)
    alias = args.get("alias")

    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    try:
        status = ops.get_status(alias)
        return _mcp_response({"success": True, **status})
    except ValueError:
        return _mcp_response(
            {"success": False, "error": f"Global repo '{alias}' not found"}
        )


def handle_get_global_config(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_global_config tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_repos_dir = _get_golden_repos_dir()
    ops = GlobalRepoOperations(golden_repos_dir)
    config = ops.get_config()
    return _mcp_response({"success": True, **config})


def handle_set_global_config(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for set_global_config tool."""
    from code_indexer.global_repos.shared_operations import GlobalRepoOperations

    golden_repos_dir = _get_golden_repos_dir()
    ops = GlobalRepoOperations(golden_repos_dir)
    refresh_interval = args.get("refresh_interval")

    if not refresh_interval:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: refresh_interval"}
        )

    try:
        ops.set_config(refresh_interval)
        return _mcp_response(
            {"success": True, "status": "updated", "refresh_interval": refresh_interval}
        )
    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})


def handle_add_golden_repo_index(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for add_golden_repo_index tool (Story #596 AC1, AC3, AC4, AC5)."""
    alias = args.get("alias")
    index_type = args.get("index_type")

    # Validate required parameters
    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    if not index_type:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: index_type"}
        )

    try:
        # Get GoldenRepoManager from app state
        golden_repo_manager = getattr(app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            return _mcp_response(
                {"success": False, "error": "Golden repository manager not available"}
            )

        # Call backend method to submit background job
        job_id = golden_repo_manager.add_index_to_golden_repo(
            alias=alias, index_type=index_type, submitter_username=user.username
        )

        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "message": f"Index type '{index_type}' is being added to golden repo '{alias}'. Use get_job_statistics to track progress.",
            }
        )

    except ValueError as e:
        # AC4: Unknown alias, AC3: Invalid type, AC5: Already exists
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-037",
                f"Error adding index to golden repo: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Failed to add index: {str(e)}"}
        )


def handle_get_golden_repo_indexes(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_golden_repo_indexes tool (Story #596 AC2, AC4)."""
    alias = args.get("alias")

    # Validate required parameter
    if not alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: alias"}
        )

    try:
        # Get GoldenRepoManager from app state
        golden_repo_manager = getattr(app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            return _mcp_response(
                {"success": False, "error": "Golden repository manager not available"}
            )

        # Get index status from backend
        status = golden_repo_manager.get_golden_repo_indexes(alias)

        return _mcp_response({"success": True, **status})

    except ValueError as e:
        # AC4: Unknown alias
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-038",
                f"Error getting golden repo indexes: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Failed to get indexes: {str(e)}"}
        )


async def _omni_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-regex search across multiple repositories."""
    import json as json_module
    import time

    repo_aliases = args.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "matches": [],
                "total_matches": 0,
                "truncated": False,
                "search_engine": "ripgrep",
                "search_time_ms": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    start_time = time.time()
    all_matches = []
    errors = {}
    repos_searched = 0
    truncated = False

    for repo_alias in repo_aliases:
        try:
            single_args = dict(args)
            single_args["repository_alias"] = repo_alias

            single_result = await handle_regex_search(single_args, user)

            content = single_result.get("content", [])
            if content and content[0].get("type") == "text":
                result_data = json_module.loads(content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    matches = result_data.get("matches", [])
                    for m in matches:
                        m["source_repo"] = repo_alias
                    all_matches.extend(matches)
                    if result_data.get("truncated"):
                        truncated = True
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-039",
                    f"Omni-regex failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    elapsed_ms = int((time.time() - start_time) * 1000)

    response_format = args.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_matches,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    formatted["truncated"] = truncated
    formatted["search_engine"] = "ripgrep"
    formatted["search_time_ms"] = elapsed_ms
    if response_format == "flat":
        formatted["matches"] = formatted.pop("results")
        formatted["total_matches"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


async def handle_regex_search(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for regex_search tool - pattern matching with timeout protection."""
    from pathlib import Path
    from code_indexer.global_repos.regex_search import RegexSearchService
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.services.search_error_formatter import SearchErrorFormatter

    # Story #4 AC2: Metrics tracking moved to service layer
    # RegexSearchService.search() now handles metrics
    # This ensures both MCP and REST API calls are counted

    repository_alias = args.get("repository_alias")
    repository_alias = _parse_json_string_array(repository_alias)
    args["repository_alias"] = repository_alias  # Update args for downstream

    # Bug #139: Validate include/exclude patterns BEFORE routing to omni-search
    # This ensures validation runs for both single-repo and omni-search modes
    include_patterns = args.get("include_patterns")
    if include_patterns is not None and not isinstance(include_patterns, list):
        return _mcp_response(
            {"success": False, "error": "include_patterns must be a list of strings"}
        )

    exclude_patterns = args.get("exclude_patterns")
    if exclude_patterns is not None and not isinstance(exclude_patterns, list):
        return _mcp_response(
            {"success": False, "error": "exclude_patterns must be a list of strings"}
        )

    # Route to omni-search when repository_alias is an array
    if isinstance(repository_alias, list):
        return await _omni_regex_search(args, user)

    pattern = args.get("pattern")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not pattern:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: pattern"}
        )

    try:
        golden_repos_dir = _get_golden_repos_dir()

        # Resolve repository_alias to actual repo path (not index path)
        # Uses _resolve_repo_path which handles all location variants
        resolved = _resolve_repo_path(repository_alias, golden_repos_dir)
        if not resolved:
            return _mcp_response(
                {"success": False, "error": "Repository '.*' not found"}
            )
        repo_path = Path(resolved)

        # Get search limits configuration from consolidated config
        config_service = get_config_service()
        config = config_service.get_config()
        search_limits = config.search_limits_config
        # Story #27: Get subprocess_max_workers from background_jobs_config
        subprocess_max_workers = config.background_jobs_config.subprocess_max_workers

        # Create service and execute search with timeout protection
        service = RegexSearchService(
            repo_path, subprocess_max_workers=subprocess_max_workers
        )
        result = await service.search(
            pattern=pattern,
            path=args.get("path"),
            include_patterns=args.get("include_patterns"),
            exclude_patterns=args.get("exclude_patterns"),
            case_sensitive=args.get("case_sensitive", True),
            context_lines=args.get("context_lines", 0),
            max_results=args.get("max_results", 100),
            timeout_seconds=search_limits.timeout_seconds,
        )

        # Convert dataclass to dict for JSON serialization
        matches = [
            {
                "file_path": m.file_path,
                "line_number": m.line_number,
                "column": m.column,
                "line_content": m.line_content,
                "context_before": m.context_before,
                "context_after": m.context_after,
            }
            for m in result.matches
        ]

        # Story #292: Enrich matches with wiki_url for .md files from wiki-enabled repos (AC1, AC4)
        wiki_enabled_repos = _get_wiki_enabled_repos()
        for match in matches:
            _enrich_with_wiki_url(
                match,
                match.get("file_path", ""),
                repository_alias,
                wiki_enabled_repos,
            )

        # Story #684: Apply payload truncation to regex search results
        # Story #50: Truncation functions are now sync
        matches = _apply_regex_payload_truncation(matches)

        return _mcp_response(
            {
                "success": True,
                "matches": matches,
                "total_matches": result.total_matches,
                "truncated": result.truncated,
                "search_engine": result.search_engine,
                "search_time_ms": result.search_time_ms,
            }
        )

    except TimeoutError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-040",
                f"Search timeout in regex_search: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        # Format timeout error response
        error_formatter = SearchErrorFormatter()
        config_service = get_config_service()
        search_limits = config_service.get_config().search_limits_config
        error_data = error_formatter.format_timeout_error(
            timeout_seconds=search_limits.timeout_seconds,
            partial_results=None,
        )
        return _mcp_response({"success": False, **error_data})

    except Exception as e:
        logger.exception(
            f"Error in regex_search: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# =============================================================================
# File CRUD Handlers (Story #628)
# =============================================================================


def handle_create_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Create new file in activated repository.

    Args:
        params: Dictionary with repository_alias, file_path, content
        user: User performing the operation

    Returns:
        MCP response with file metadata including content_hash for optimistic locking
    """
    from code_indexer.server.services.file_crud_service import (
        file_crud_service,
        CRUDOperationError,
    )
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    try:
        # Validate required parameters
        repository_alias = params.get("repository_alias")
        file_path = params.get("file_path")
        content = params.get("content")

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )
        if not file_path:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: file_path"}
            )
        if content is None:  # Allow empty string content
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: content"}
            )

        # Start auto-watch before file creation (Story #640, Story #197 AC3)
        # Skip during write mode — write mode manages the full indexing lifecycle
        # via exit_write_mode / _write_mode_run_refresh (Bug #274 Bug 1).
        try:
            # Check if this is a write exception (e.g., cidx-meta-global)
            if file_crud_service.is_write_exception(repository_alias):
                # Use canonical golden repo path for exceptions
                repo_path = str(file_crud_service.get_write_exception_path(repository_alias))
            else:
                # Use activated repo path for normal repos
                activated_repo_manager = ActivatedRepoManager()
                repo_path = activated_repo_manager.get_activated_repo_path(
                    username=user.username, user_alias=repository_alias
                )
            golden_repos_dir = getattr(app_module.app.state, "golden_repos_dir", None)
            if not _is_write_mode_active(repository_alias, golden_repos_dir):
                auto_watch_manager.start_watch(repo_path)
            else:
                logger.debug(
                    f"Skipping auto-watch start for {repository_alias}: write mode active",
                    extra={"correlation_id": get_correlation_id()},
                )
        except Exception as e:
            # Log but don't fail - auto-watch is enhancement, not critical
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-041",
                    f"Failed to start auto-watch for {repository_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Call file CRUD service
        result = file_crud_service.create_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content=content,
            username=user.username,
        )

        return _mcp_response(result)

    except FileExistsError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-042",
                f"File creation failed - file already exists: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-043",
                f"File creation failed - permission denied: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except CRUDOperationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-044",
                f"File creation failed - CRUD operation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-045",
                f"File creation failed - invalid parameters: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_create_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_edit_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Edit file using exact string replacement with optimistic locking.

    Args:
        params: Dictionary with repository_alias, file_path, old_string, new_string,
                content_hash, and optional replace_all
        user: User performing the operation

    Returns:
        MCP response with new content_hash and change metadata
    """
    from code_indexer.server.services.file_crud_service import (
        file_crud_service,
        HashMismatchError,
        CRUDOperationError,
    )
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    try:
        # Validate required parameters
        repository_alias = params.get("repository_alias")
        file_path = params.get("file_path")
        old_string = params.get("old_string")
        new_string = params.get("new_string")
        content_hash = params.get("content_hash")
        replace_all = params.get("replace_all", False)

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )
        if not file_path:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: file_path"}
            )
        if old_string is None:  # Allow empty string
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: old_string"}
            )
        if new_string is None:  # Allow empty string
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: new_string"}
            )
        if not content_hash:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: content_hash"}
            )

        # Start auto-watch before file edit (Story #640, Story #197 AC3)
        # Skip during write mode — write mode manages the full indexing lifecycle
        # via exit_write_mode / _write_mode_run_refresh (Bug #274 Bug 1).
        try:
            # Check if this is a write exception (e.g., cidx-meta-global)
            if file_crud_service.is_write_exception(repository_alias):
                # Use canonical golden repo path for exceptions
                repo_path = str(file_crud_service.get_write_exception_path(repository_alias))
            else:
                # Use activated repo path for normal repos
                activated_repo_manager = ActivatedRepoManager()
                repo_path = activated_repo_manager.get_activated_repo_path(
                    username=user.username, user_alias=repository_alias
                )
            golden_repos_dir = getattr(app_module.app.state, "golden_repos_dir", None)
            if not _is_write_mode_active(repository_alias, golden_repos_dir):
                auto_watch_manager.start_watch(repo_path)
            else:
                logger.debug(
                    f"Skipping auto-watch start for {repository_alias}: write mode active",
                    extra={"correlation_id": get_correlation_id()},
                )
        except Exception as e:
            # Log but don't fail - auto-watch is enhancement, not critical
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-046",
                    f"Failed to start auto-watch for {repository_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Call file CRUD service
        result = file_crud_service.edit_file(
            repo_alias=repository_alias,
            file_path=file_path,
            old_string=old_string,
            new_string=new_string,
            content_hash=content_hash,
            replace_all=replace_all,
            username=user.username,
        )

        return _mcp_response(result)

    except HashMismatchError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-047",
                f"File edit failed - hash mismatch (concurrent modification): {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except FileNotFoundError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-048",
                f"File edit failed - file not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-049",
                f"File edit failed - validation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-050",
                f"File edit failed - permission denied: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except CRUDOperationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-051",
                f"File edit failed - CRUD operation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_edit_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_delete_file(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Delete file from activated repository.

    Args:
        params: Dictionary with repository_alias, file_path, and optional content_hash
        user: User performing the operation

    Returns:
        MCP response with deletion confirmation
    """
    from code_indexer.server.services.file_crud_service import (
        file_crud_service,
        HashMismatchError,
        CRUDOperationError,
    )
    from code_indexer.server.services.auto_watch_manager import auto_watch_manager
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    try:
        # Validate required parameters
        repository_alias = params.get("repository_alias")
        file_path = params.get("file_path")
        content_hash = params.get("content_hash")  # Optional

        if not repository_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: repository_alias",
                }
            )
        if not file_path:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: file_path"}
            )

        # Start auto-watch before file deletion (Story #640, Story #197 AC3)
        # Skip during write mode — write mode manages the full indexing lifecycle
        # via exit_write_mode / _write_mode_run_refresh (Bug #274 Bug 1).
        try:
            # Check if this is a write exception (e.g., cidx-meta-global)
            if file_crud_service.is_write_exception(repository_alias):
                # Use canonical golden repo path for exceptions
                repo_path = str(file_crud_service.get_write_exception_path(repository_alias))
            else:
                # Use activated repo path for normal repos
                activated_repo_manager = ActivatedRepoManager()
                repo_path = activated_repo_manager.get_activated_repo_path(
                    username=user.username, user_alias=repository_alias
                )
            golden_repos_dir = getattr(app_module.app.state, "golden_repos_dir", None)
            if not _is_write_mode_active(repository_alias, golden_repos_dir):
                auto_watch_manager.start_watch(repo_path)
            else:
                logger.debug(
                    f"Skipping auto-watch start for {repository_alias}: write mode active",
                    extra={"correlation_id": get_correlation_id()},
                )
        except Exception as e:
            # Log but don't fail - auto-watch is enhancement, not critical
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-052",
                    f"Failed to start auto-watch for {repository_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

        # Call file CRUD service
        result = file_crud_service.delete_file(
            repo_alias=repository_alias,
            file_path=file_path,
            content_hash=content_hash,
            username=user.username,
        )

        return _mcp_response(result)

    except HashMismatchError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-053",
                f"File deletion failed - hash mismatch (safety check): {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except FileNotFoundError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-054",
                f"File deletion failed - file not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except PermissionError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-055",
                f"File deletion failed - permission denied: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except CRUDOperationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-056",
                f"File deletion failed - CRUD operation error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except ValueError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-057",
                f"File deletion failed - invalid parameters: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in handle_delete_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Write-mode helpers (Story #231)
# ---------------------------------------------------------------------------


def _write_mode_strip_global(repo_alias: str) -> str:
    """Return alias without trailing '-global' suffix."""
    return repo_alias[: -len("-global")] if repo_alias.endswith("-global") else repo_alias


def _is_write_mode_active(repo_alias: str, golden_repos_dir: Optional[str]) -> bool:
    """Return True if a write-mode marker exists for repo_alias.

    Used by file operation handlers to suppress auto-watch during write mode.
    During write mode, the caller (enter_write_mode / exit_write_mode) manages
    the full indexing lifecycle, so auto-watch must not race with it.

    Args:
        repo_alias: Repository alias (with or without -global suffix)
        golden_repos_dir: Path to the golden repos root directory (may be None)

    Returns:
        True if write mode marker file exists for this repo alias, False otherwise
    """
    if not golden_repos_dir:
        return False
    alias = _write_mode_strip_global(repo_alias)
    marker_file = Path(golden_repos_dir) / ".write_mode" / f"{alias}.json"
    return marker_file.exists()


def _write_mode_acquire_lock(refresh_scheduler: Any, alias: str) -> Tuple[bool, str]:
    """Acquire write lock; return (acquired, owner_if_held)."""
    acquired = refresh_scheduler.acquire_write_lock(alias, owner_name="mcp_write_mode")
    if acquired:
        return True, ""
    wlm = getattr(refresh_scheduler, "write_lock_manager", None)
    owner = "unknown"
    if wlm is not None:
        info = wlm.get_lock_info(alias)
        if info:
            owner = info.get("owner", "unknown")
    return False, owner


def _write_mode_create_marker(golden_repos_dir: Path, alias: str, source_path: str) -> None:
    """Create the .write_mode/{alias}.json marker file."""
    import json as _json
    from datetime import datetime, timezone

    write_mode_dir = golden_repos_dir / ".write_mode"
    write_mode_dir.mkdir(parents=True, exist_ok=True)
    marker_file = write_mode_dir / f"{alias}.json"
    marker_file.write_text(
        _json.dumps(
            {
                "alias": alias,
                "source_path": source_path,
                "entered_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def handle_enter_write_mode(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Enter write mode for a write-exception repo (Story #231 C1).

    No-op for non-write-exception repos. For write-exception repos: acquires
    write lock, creates marker file, returns source path.
    """
    try:
        repo_alias = params.get("repo_alias")
        if not repo_alias:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_alias"}
            )
        from code_indexer.server.services.file_crud_service import file_crud_service

        if not file_crud_service.is_write_exception(repo_alias):
            return _mcp_response(
                {"success": True, "message": f"no-op: '{repo_alias}' is not a write-exception repo"}
            )

        alias = _write_mode_strip_global(repo_alias)
        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            return _mcp_response({"success": False, "error": "RefreshScheduler not available"})

        acquired, owner = _write_mode_acquire_lock(refresh_scheduler, alias)
        if not acquired:
            return _mcp_response(
                {"success": False, "message": f"Write lock for '{alias}' is already held by '{owner}'"}
            )

        try:
            source_path = file_crud_service.get_write_exception_path(repo_alias)
            golden_repos_dir = Path(_get_golden_repos_dir())
            _write_mode_create_marker(golden_repos_dir, alias, str(source_path))
        except Exception:
            refresh_scheduler.release_write_lock(alias, owner_name="mcp_write_mode")
            raise
        logger.info(f"enter_write_mode: write mode active for '{repo_alias}', source={source_path}")
        return _mcp_response({"success": True, "alias": repo_alias, "source_path": str(source_path)})
    except Exception as e:
        logger.exception(f"Unexpected error in handle_enter_write_mode: {e}",
                         extra={"correlation_id": get_correlation_id()})
        return _mcp_response({"success": False, "error": str(e)})


def _write_mode_run_refresh(
    refresh_scheduler: Any, repo_alias: str, golden_repos_dir: Path, alias: str
) -> None:
    """Run synchronous refresh, delete marker, release lock, stop auto-watch.

    The write lock and marker are released BEFORE calling _execute_refresh so
    that _execute_refresh does not see the lock as held and skip the refresh.
    (_execute_refresh checks is_write_locked() and skips for local repos when
    the lock is held — Story #227 guard for external writers.)

    The auto-watch is stopped BEFORE _execute_refresh to prevent the watch
    handler from racing with the refresh (Bug #274 Bug 3).

    The exception from _execute_refresh is re-raised so the caller can return
    an appropriate error response.
    """
    import json as _json

    from code_indexer.server.services.auto_watch_manager import auto_watch_manager

    # Read source_path from marker BEFORE deleting it — needed to stop auto-watch
    marker_file = golden_repos_dir / ".write_mode" / f"{alias}.json"
    source_path: Optional[str] = None
    try:
        marker_data = _json.loads(marker_file.read_text())
        source_path = marker_data.get("source_path")
    except Exception:
        pass  # marker may already be gone or unreadable — proceed without source_path

    # Release marker and lock FIRST so _execute_refresh sees no lock
    try:
        marker_file.unlink()
    except FileNotFoundError:
        pass
    refresh_scheduler.release_write_lock(alias, owner_name="mcp_write_mode")

    # Stop auto-watch for the repo BEFORE running refresh (Bug #274 Bug 3)
    # This prevents the watch handler from processing events that the refresh
    # will handle, avoiding hash mismatches and wasted VoyageAI API calls.
    if source_path:
        try:
            auto_watch_manager.stop_watch(source_path)
            logger.debug(
                f"Stopped auto-watch for {source_path} before write-mode refresh",
                extra={"correlation_id": get_correlation_id()},
            )
        except Exception as e:
            logger.warning(
                f"Failed to stop auto-watch for {source_path} before refresh: {e}",
                extra={"correlation_id": get_correlation_id()},
            )

    # Now run the refresh — lock is clear, watch is stopped, refresh will proceed
    refresh_scheduler._execute_refresh(repo_alias)


def handle_exit_write_mode(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Exit write mode for a write-exception repo (Story #231 C2).

    No-op for non-write-exception repos. For write-exception repos: triggers
    synchronous refresh, removes marker, releases lock.
    """
    try:
        repo_alias = params.get("repo_alias")
        if not repo_alias:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_alias"}
            )
        from code_indexer.server.services.file_crud_service import file_crud_service

        if not file_crud_service.is_write_exception(repo_alias):
            return _mcp_response(
                {"success": True, "message": f"no-op: '{repo_alias}' is not a write-exception repo"}
            )

        alias = _write_mode_strip_global(repo_alias)
        golden_repos_dir = Path(_get_golden_repos_dir())
        marker_file = golden_repos_dir / ".write_mode" / f"{alias}.json"

        if not marker_file.exists():
            logger.warning(f"exit_write_mode: no marker for '{repo_alias}' — not in write mode")
            return _mcp_response(
                {"success": True, "warning": f"Write mode was not active for '{repo_alias}'",
                 "message": "not in write mode — nothing to exit"}
            )

        refresh_scheduler = _get_app_refresh_scheduler()
        if refresh_scheduler is None:
            return _mcp_response({"success": False, "error": "RefreshScheduler not available"})

        logger.info(f"exit_write_mode: triggering synchronous refresh for '{repo_alias}'")
        _write_mode_run_refresh(refresh_scheduler, repo_alias, golden_repos_dir, alias)
        logger.info(f"exit_write_mode: write mode exited for '{repo_alias}', refresh complete")
        return _mcp_response(
            {"success": True, "message": f"Refresh complete, write mode exited for '{repo_alias}'"}
        )
    except Exception as e:
        logger.exception(f"Unexpected error in handle_exit_write_mode: {e}",
                         extra={"correlation_id": get_correlation_id()})
        return _mcp_response({"success": False, "error": str(e)})


# Handler registry mapping tool names to handler functions
# Type: Dict[str, Any] because handlers have varying signatures (2-param vs 3-param)
HANDLER_REGISTRY: Dict[str, Any] = {
    "search_code": search_code,
    "discover_repositories": discover_repositories,
    "list_repositories": list_repositories,
    "activate_repository": activate_repository,
    "deactivate_repository": deactivate_repository,
    "get_repository_status": get_repository_status,
    "sync_repository": sync_repository,
    "switch_branch": switch_branch,
    "list_files": list_files,
    "get_file_content": get_file_content,
    "browse_directory": browse_directory,
    "get_branches": get_branches,
    "check_health": check_health,
    "check_hnsw_health": check_hnsw_health,
    "add_golden_repo": add_golden_repo,
    "remove_golden_repo": remove_golden_repo,
    "refresh_golden_repo": refresh_golden_repo,
    "list_users": list_users,
    "create_user": create_user,
    "get_repository_statistics": get_repository_statistics,
    "get_job_statistics": get_job_statistics,
    "get_job_details": get_job_details,
    "get_all_repositories_status": get_all_repositories_status,
    "manage_composite_repository": manage_composite_repository,
    "list_global_repos": handle_list_global_repos,
    "global_repo_status": handle_global_repo_status,
    "get_global_config": handle_get_global_config,
    "set_global_config": handle_set_global_config,
    "add_golden_repo_index": handle_add_golden_repo_index,
    "get_golden_repo_indexes": handle_get_golden_repo_indexes,
    "regex_search": handle_regex_search,
    "create_file": handle_create_file,
    "edit_file": handle_edit_file,
    "delete_file": handle_delete_file,
    "enter_write_mode": handle_enter_write_mode,
    "exit_write_mode": handle_exit_write_mode,
}


def _omni_git_log(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-git-log across multiple repositories."""
    import json as json_module

    repo_aliases = args.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases)
    limit = _coerce_int(args.get("limit"), 20)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "commits": [],
                "total_count": 0,
                "truncated": False,
                "repos_searched": 0,
                "errors": {},
            }
        )

    all_commits = []
    errors = {}
    repos_searched = 0
    truncated = False

    per_repo_limit = max(1, limit // len(repo_aliases)) if repo_aliases else limit

    for repo_alias in repo_aliases:
        try:
            single_args = dict(args)
            single_args["repository_alias"] = repo_alias
            single_args["limit"] = per_repo_limit

            single_result = handle_git_log(single_args, user)

            resp_content = single_result.get("content", [])
            if resp_content and resp_content[0].get("type") == "text":
                result_data = json_module.loads(resp_content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    commits = result_data.get("commits", [])
                    for c in commits:
                        c["source_repo"] = repo_alias
                    all_commits.extend(commits)
                    if result_data.get("truncated"):
                        truncated = True
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-058",
                    f"Omni-git-log failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    # Sort by date descending and apply limit
    all_commits.sort(key=lambda x: x.get("date", ""), reverse=True)
    final_commits = all_commits[:limit]

    response_format = args.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=final_commits,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    formatted["truncated"] = truncated or len(all_commits) > limit
    if response_format == "flat":
        formatted["commits"] = formatted.pop("results")
        formatted["total_count"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


def handle_git_log(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_log tool - retrieve commit history from a repository.

    Story #35: When log exceeds git_log_max_tokens, stores full log in
    PayloadCache and returns cache_handle for paginated retrieval.
    """
    import json as json_module
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    repository_alias = _parse_json_string_array(repository_alias)
    args["repository_alias"] = repository_alias  # Update args for downstream

    # Route to omni-search when repository_alias is an array
    if isinstance(repository_alias, list):
        return _omni_git_log(args, user)

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute query
        service = GitOperationsService(Path(repo_path))
        result = service.get_log(
            limit=_coerce_int(args.get("limit"), 50),
            path=args.get("path"),
            author=args.get("author"),
            since=args.get("since"),
            until=args.get("until"),
            branch=args.get("branch"),
        )

        # Convert dataclasses to dicts for JSON serialization
        commits = [
            {
                "hash": c.hash,
                "short_hash": c.short_hash,
                "author_name": c.author_name,
                "author_email": c.author_email,
                "author_date": c.author_date,
                "committer_name": c.committer_name,
                "committer_email": c.committer_email,
                "committer_date": c.committer_date,
                "subject": c.subject,
                "body": c.body,
            }
            for c in result.commits
        ]

        # Story #35: Build full log result for potential caching
        full_log_data = {
            "commits": commits,
            "total_count": result.total_count,
        }

        # Story #35: Apply token-based truncation with cache handle support
        # Get payload cache and content limits config
        payload_cache = getattr(app_module.app.state, "payload_cache", None)
        config_service = get_config_service()
        content_limits = config_service.get_config().content_limits_config

        # Initialize truncation fields
        cache_handle = None
        truncated = False
        total_tokens = 0
        preview_tokens = 0
        total_pages = 0
        has_more = False

        # Serialize log result to JSON for token counting and caching
        log_json = json_module.dumps(full_log_data)

        if payload_cache is not None and log_json and content_limits is not None:
            from code_indexer.server.cache.truncation_helper import TruncationHelper

            truncation_helper = TruncationHelper(payload_cache, content_limits)
            truncation_result = truncation_helper.truncate_and_cache(
                content=log_json,
                content_type="log",
            )

            cache_handle = truncation_result.cache_handle
            truncated = truncation_result.truncated
            total_tokens = truncation_result.original_tokens
            preview_tokens = truncation_result.preview_tokens
            total_pages = truncation_result.total_pages
            has_more = truncation_result.has_more

            # Note: We still return all commits in the response for backward compatibility.
            # The cache_handle allows clients to retrieve the full serialized JSON if needed.

        return _mcp_response(
            {
                "success": True,
                "commits": commits,
                "total_count": result.total_count,
                # Story #35: Truncation metadata fields
                "cache_handle": cache_handle,
                "truncated": truncated,
                "total_tokens": total_tokens,
                "preview_tokens": preview_tokens,
                "total_pages": total_pages,
                "has_more": has_more,
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in git_log: {e}", extra={"correlation_id": get_correlation_id()}
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_show_commit(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_show_commit tool - get detailed commit information."""
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    commit_hash = args.get("commit_hash")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not commit_hash:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: commit_hash"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute query
        service = GitOperationsService(Path(repo_path))
        result = service.show_commit(
            commit_hash=commit_hash,
            include_diff=args.get("include_diff", False),
            include_stats=args.get("include_stats", True),
        )

        # Convert dataclasses to dicts for JSON serialization
        commit_dict = {
            "hash": result.commit.hash,
            "short_hash": result.commit.short_hash,
            "author_name": result.commit.author_name,
            "author_email": result.commit.author_email,
            "author_date": result.commit.author_date,
            "committer_name": result.commit.committer_name,
            "committer_email": result.commit.committer_email,
            "committer_date": result.commit.committer_date,
            "subject": result.commit.subject,
            "body": result.commit.body,
        }

        stats_list = None
        if result.stats is not None:
            stats_list = [
                {
                    "path": s.path,
                    "insertions": s.insertions,
                    "deletions": s.deletions,
                    "status": s.status,
                }
                for s in result.stats
            ]

        return _mcp_response(
            {
                "success": True,
                "commit": commit_dict,
                "stats": stats_list,
                "diff": result.diff,
                "parents": result.parents,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_show_commit: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_file_at_revision(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_file_at_revision tool - get file contents at specific revision."""
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    path = args.get("path")
    revision = args.get("revision")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: path"}
        )
    if not revision:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: revision"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute query
        service = GitOperationsService(Path(repo_path))
        result = service.get_file_at_revision(path=path, revision=revision)

        return _mcp_response(
            {
                "success": True,
                "path": result.path,
                "revision": result.revision,
                "resolved_revision": result.resolved_revision,
                "content": result.content,
                "size_bytes": result.size_bytes,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_file_at_revision: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def _is_git_repo(path: Path) -> bool:
    """Check if path is a valid git repository."""
    return path.exists() and (path / ".git").exists()


def _find_latest_versioned_repo(base_path: Path, repo_name: str) -> Optional[str]:
    """Find most recent versioned git repo in .versioned/{name}/v_*/ structure."""
    versioned_base = base_path / ".versioned" / repo_name
    if not versioned_base.exists():
        return None

    version_dirs = sorted(
        [d for d in versioned_base.iterdir() if d.is_dir() and d.name.startswith("v_")],
        key=lambda d: d.name,
        reverse=True,
    )

    for version_dir in version_dirs:
        if _is_git_repo(version_dir):
            return str(version_dir)

    return None


def _resolve_repo_path(repo_identifier: str, golden_repos_dir: str) -> Optional[str]:
    """Resolve repository identifier to filesystem path with actual git repo.

    Searches multiple locations to find a directory with .git:
    1. The index_path from registry (if it has .git)
    2. The golden-repos/{name} directory
    3. The golden-repos/repos/{name} directory
    4. Versioned repos in .versioned/{name}/v_*/

    Args:
        repo_identifier: Repository alias or path
        golden_repos_dir: Path to golden repos directory

    Returns:
        Filesystem path to git repository, or None if not found
    """
    # Try as full path first
    if not repo_identifier.endswith("-global"):
        repo_path = Path(repo_identifier)
        if _is_git_repo(repo_path):
            return str(repo_path)

    # Look up in global registry
    registry = get_server_global_registry(golden_repos_dir)
    repo_entry = registry.get_global_repo(repo_identifier)

    if not repo_entry:
        return None

    # Get repo name without -global suffix
    repo_name = repo_identifier.replace("-global", "")

    # Try 1: index_path directly (might be a git repo in test environments)
    index_path = repo_entry.get("index_path")
    if index_path:
        index_path_obj = Path(index_path)
        if _is_git_repo(index_path_obj):
            return str(index_path)

    # Get base directory (.cidx-server/)
    base_dir = Path(golden_repos_dir).parent.parent

    # Try 2: Check golden-repos/{name}
    alt_path = base_dir / "golden-repos" / repo_name
    if _is_git_repo(alt_path):
        return str(alt_path)

    # Try 3: Check golden-repos/repos/{name}
    alt_path = base_dir / "golden-repos" / "repos" / repo_name
    if _is_git_repo(alt_path):
        return str(alt_path)

    # Try 4: Check versioned repos in data/golden-repos/.versioned
    versioned_path = _find_latest_versioned_repo(Path(golden_repos_dir), repo_name)
    if versioned_path:
        return versioned_path

    # Try 5: Check versioned repos in alternative location
    versioned_path = _find_latest_versioned_repo(
        base_dir / "data" / "golden-repos", repo_name
    )
    if versioned_path:
        return versioned_path

    # Fallback: Return index_path if it exists as a directory (for non-git operations like regex_search)
    if index_path:
        index_path_obj = Path(index_path)
        if index_path_obj.is_dir():
            return str(index_path)

    return None


def _resolve_git_repo_path(
    repository_alias: str, username: str
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve repository path for git operations.

    For global repos (ending in -global): validates that the resolved path
    has a .git directory. Local repos (e.g. cidx-meta-global backed by
    local://) have no .git and git operations are not meaningful.

    For user-activated repos: returns the activated-repo path if it exists.

    Returns:
        (path, error_message) tuple. If error_message is not None,
        the caller should return the error to the user.
    """
    if repository_alias.endswith("-global"):
        golden_repos_dir = _get_golden_repos_dir()

        # Check repo URL first — local:// repos never support git operations
        registry = get_server_global_registry(golden_repos_dir)
        repo_entry = registry.get_global_repo(repository_alias)
        if repo_entry and repo_entry.get("repo_url", "").startswith("local://"):
            return None, (
                f"Repository '{repository_alias}' is a local repository "
                "and does not support git operations."
            )

        resolved = _resolve_repo_path(repository_alias, golden_repos_dir)
        if resolved is None:
            return None, f"Repository '{repository_alias}' not found."
        if not (Path(resolved) / ".git").exists():
            return None, (
                f"Repository '{repository_alias}' is a local repository "
                "and does not support git operations."
            )
        return resolved, None

    activated_repo_manager = ActivatedRepoManager()
    repo_path = activated_repo_manager.get_activated_repo_path(
        username=username, user_alias=repository_alias
    )
    if repo_path is None:
        return None, f"User-activated repository '{repository_alias}' not found."
    if not (Path(repo_path) / ".git").exists():
        return None, (
            f"Repository '{repository_alias}' does not have a .git directory "
            "and does not support git operations."
        )
    return repo_path, None


# Update handler registry with git exploration tools
HANDLER_REGISTRY["git_log"] = handle_git_log
HANDLER_REGISTRY["git_show_commit"] = handle_git_show_commit
HANDLER_REGISTRY["git_file_at_revision"] = handle_git_file_at_revision


# Story #555: Git Diff and Blame handlers
# Story #34: Git Diff Returns Cache Handle on Truncation
def handle_git_diff(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_diff tool - get diff between revisions.

    Story #34: When diff exceeds git_diff_max_tokens, stores full diff in
    PayloadCache and returns cache_handle for paginated retrieval.
    """
    import json as json_module
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    from_revision = args.get("from_revision")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not from_revision:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: from_revision"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute query
        service = GitOperationsService(Path(repo_path))
        result = service.get_diff(
            from_revision=from_revision,
            to_revision=args.get("to_revision"),
            path=args.get("path"),
            context_lines=args.get("context_lines", 3),
            stat_only=args.get("stat_only", False),
        )

        # Convert dataclasses to dicts for JSON serialization
        files = [
            {
                "path": f.path,
                "old_path": f.old_path,
                "status": f.status,
                "insertions": f.insertions,
                "deletions": f.deletions,
                "hunks": [
                    {
                        "old_start": h.old_start,
                        "old_count": h.old_count,
                        "new_start": h.new_start,
                        "new_count": h.new_count,
                        "content": h.content,
                    }
                    for h in f.hunks
                ],
            }
            for f in result.files
        ]

        # Story #34: Build full diff result for potential caching
        full_diff_data = {
            "from_revision": result.from_revision,
            "to_revision": result.to_revision,
            "files": files,
            "total_insertions": result.total_insertions,
            "total_deletions": result.total_deletions,
            "stat_summary": result.stat_summary,
        }

        # Story #34: Apply token-based truncation with cache handle support
        # Get payload cache and content limits config
        payload_cache = getattr(app_module.app.state, "payload_cache", None)
        config_service = get_config_service()
        content_limits = config_service.get_config().content_limits_config

        # Initialize truncation fields
        cache_handle = None
        truncated = False
        total_tokens = 0
        preview_tokens = 0
        total_pages = 0
        has_more = False

        # Serialize diff result to JSON for token counting and caching
        diff_json = json_module.dumps(full_diff_data)

        if payload_cache is not None and diff_json and content_limits is not None:
            from code_indexer.server.cache.truncation_helper import TruncationHelper

            truncation_helper = TruncationHelper(payload_cache, content_limits)
            truncation_result = truncation_helper.truncate_and_cache(
                content=diff_json,
                content_type="diff",
            )

            cache_handle = truncation_result.cache_handle
            truncated = truncation_result.truncated
            total_tokens = truncation_result.original_tokens
            preview_tokens = truncation_result.preview_tokens
            total_pages = truncation_result.total_pages
            has_more = truncation_result.has_more

            # Note: We still return all files in the response for backward compatibility.
            # The cache_handle allows clients to retrieve the full serialized JSON if needed.

        return _mcp_response(
            {
                "success": True,
                "from_revision": result.from_revision,
                "to_revision": result.to_revision,
                "files": files,
                "total_insertions": result.total_insertions,
                "total_deletions": result.total_deletions,
                "stat_summary": result.stat_summary,
                # Story #34: Truncation metadata fields
                "cache_handle": cache_handle,
                "truncated": truncated,
                "total_tokens": total_tokens,
                "preview_tokens": preview_tokens,
                "total_pages": total_pages,
                "has_more": has_more,
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in git_diff: {e}", extra={"correlation_id": get_correlation_id()}
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_blame(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_blame tool - get line-by-line blame annotations."""
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    path = args.get("path")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: path"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute query
        service = GitOperationsService(Path(repo_path))
        result = service.get_blame(
            path=path,
            revision=args.get("revision"),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        )

        # Convert dataclasses to dicts for JSON serialization
        lines = [
            {
                "line_number": line.line_number,
                "commit_hash": line.commit_hash,
                "short_hash": line.short_hash,
                "author_name": line.author_name,
                "author_email": line.author_email,
                "author_date": line.author_date,
                "original_line_number": line.original_line_number,
                "content": line.content,
            }
            for line in result.lines
        ]

        return _mcp_response(
            {
                "success": True,
                "path": result.path,
                "revision": result.revision,
                "lines": lines,
                "unique_commits": result.unique_commits,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_blame: {e}", extra={"correlation_id": get_correlation_id()}
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_file_history(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_file_history tool - get commit history for a file."""
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    path = args.get("path")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: path"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute query
        service = GitOperationsService(Path(repo_path))
        result = service.get_file_history(
            path=path,
            limit=_coerce_int(args.get("limit"), 50),
            follow_renames=args.get("follow_renames", True),
        )

        # Convert dataclasses to dicts for JSON serialization
        commits = [
            {
                "hash": c.hash,
                "short_hash": c.short_hash,
                "author_name": c.author_name,
                "author_date": c.author_date,
                "subject": c.subject,
                "insertions": c.insertions,
                "deletions": c.deletions,
                "old_path": c.old_path,
            }
            for c in result.commits
        ]

        return _mcp_response(
            {
                "success": True,
                "path": result.path,
                "commits": commits,
                "total_count": result.total_count,
                "truncated": result.truncated,
                "renamed_from": result.renamed_from,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_file_history: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Update handler registry with git diff/blame tools (Story #555)
HANDLER_REGISTRY["git_diff"] = handle_git_diff
HANDLER_REGISTRY["git_blame"] = handle_git_blame
HANDLER_REGISTRY["git_file_history"] = handle_git_file_history


def _omni_git_search_commits(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handle omni-git-search across multiple repositories."""
    import json as json_module
    import time

    repo_aliases = args.get("repository_alias", [])
    repo_aliases = _expand_wildcard_patterns(repo_aliases)
    query = args.get("query", "")
    is_regex = args.get("is_regex", False)

    if not repo_aliases:
        return _mcp_response(
            {
                "success": True,
                "query": query,
                "is_regex": is_regex,
                "matches": [],
                "total_matches": 0,
                "truncated": False,
                "search_time_ms": 0,
                "repos_searched": 0,
                "errors": {},
            }
        )

    start_time = time.time()
    all_matches = []
    errors = {}
    repos_searched = 0
    truncated = False

    for repo_alias in repo_aliases:
        try:
            single_args = dict(args)
            single_args["repository_alias"] = repo_alias

            single_result = handle_git_search_commits(single_args, user)

            resp_content = single_result.get("content", [])
            if resp_content and resp_content[0].get("type") == "text":
                result_data = json_module.loads(resp_content[0]["text"])
                if result_data.get("success"):
                    repos_searched += 1
                    matches = result_data.get("matches", [])
                    for m in matches:
                        m["source_repo"] = repo_alias
                    all_matches.extend(matches)
                    if result_data.get("truncated"):
                        truncated = True
                else:
                    errors[repo_alias] = result_data.get("error", "Unknown error")
        except Exception as e:
            errors[repo_alias] = str(e)
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-059",
                    f"Omni-git-search failed for {repo_alias}: {e}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    elapsed_ms = int((time.time() - start_time) * 1000)

    response_format = args.get("response_format", "flat")
    formatted = _format_omni_response(
        all_results=all_matches,
        response_format=response_format,
        total_repos_searched=repos_searched,
        errors=errors,
    )
    formatted["query"] = query
    formatted["is_regex"] = is_regex
    formatted["truncated"] = truncated
    formatted["search_time_ms"] = elapsed_ms
    if response_format == "flat":
        formatted["matches"] = formatted.pop("results")
        formatted["total_matches"] = formatted.pop("total_results")
        formatted["repos_searched"] = formatted.pop("total_repos_searched")
    return _mcp_response(formatted)


# Story #556: Git Content Search handlers
def handle_git_search_commits(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_search_commits tool - search commit messages."""
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    repository_alias = _parse_json_string_array(repository_alias)
    args["repository_alias"] = repository_alias  # Update args for downstream
    query = args.get("query")

    # Route to omni-search when repository_alias is an array
    if isinstance(repository_alias, list):
        return _omni_git_search_commits(args, user)

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )
    if not query:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: query"}
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute search
        service = GitOperationsService(Path(repo_path))
        result = service.search_commits(
            query=query,
            is_regex=args.get("is_regex", False),
            author=args.get("author"),
            since=args.get("since"),
            until=args.get("until"),
            limit=_coerce_int(args.get("limit"), 50),
        )

        # Convert dataclasses to dicts for JSON serialization
        matches = [
            {
                "hash": m.hash,
                "short_hash": m.short_hash,
                "author_name": m.author_name,
                "author_email": m.author_email,
                "author_date": m.author_date,
                "subject": m.subject,
                "body": m.body,
                "match_highlights": m.match_highlights,
            }
            for m in result.matches
        ]

        return _mcp_response(
            {
                "success": True,
                "query": result.query,
                "is_regex": result.is_regex,
                "matches": matches,
                "total_matches": result.total_matches,
                "truncated": result.truncated,
                "search_time_ms": result.search_time_ms,
            }
        )

    except Exception as e:
        logger.exception(
            f"Error in git_search_commits: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_git_search_diffs(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_search_diffs tool - search for code changes (pickaxe search)."""
    from pathlib import Path
    from code_indexer.global_repos.git_operations import GitOperationsService

    repository_alias = args.get("repository_alias")
    search_string = args.get("search_string")
    search_pattern = args.get("search_pattern")
    is_regex = args.get("is_regex", False)

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    # Determine which search parameter to use based on is_regex
    search_term = search_pattern if is_regex else search_string

    # Validate that at least one search parameter is provided
    if not search_term:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameter: search_string or search_pattern",
            }
        )

    try:
        # Resolve repository path, checking for .git directory existence
        repo_path, error_msg = _resolve_git_repo_path(
            repository_alias, user.username
        )
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Create service and execute search
        # search_diffs uses search_string for literal, search_pattern for regex
        service = GitOperationsService(Path(repo_path))
        is_regex = args.get("is_regex", False)
        if is_regex:
            result = service.search_diffs(
                search_pattern=search_term,
                is_regex=True,
                path=args.get("path"),
                since=args.get("since"),
                until=args.get("until"),
                limit=_coerce_int(args.get("limit"), 50),
            )
        else:
            result = service.search_diffs(
                search_string=search_term,
                is_regex=False,
                path=args.get("path"),
                since=args.get("since"),
                until=args.get("until"),
                limit=_coerce_int(args.get("limit"), 50),
            )

        # Convert dataclasses to dicts for JSON serialization
        matches = [
            {
                "hash": m.hash,
                "short_hash": m.short_hash,
                "author_name": m.author_name,
                "author_date": m.author_date,
                "subject": m.subject,
                "files_changed": m.files_changed,
                "diff_snippet": m.diff_snippet,
            }
            for m in result.matches
        ]

        return _mcp_response(
            {
                "success": True,
                "search_term": result.search_term,
                "is_regex": result.is_regex,
                "matches": matches,
                "total_matches": result.total_matches,
                "truncated": result.truncated,
                "search_time_ms": result.search_time_ms,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in git_search_diffs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Update handler registry with git content search tools (Story #556)
HANDLER_REGISTRY["git_search_commits"] = handle_git_search_commits
HANDLER_REGISTRY["git_search_diffs"] = handle_git_search_diffs


# Story #557: Directory Tree handler
def handle_directory_tree(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for directory_tree tool - generate hierarchical tree view."""
    from pathlib import Path
    from code_indexer.global_repos.directory_explorer import DirectoryExplorerService

    repository_alias = args.get("repository_alias")

    # Validate required parameters
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        golden_repos_dir = _get_golden_repos_dir()

        # Resolve repository_alias to actual path
        if repository_alias.endswith("-global"):
            # Use AliasManager for global repos (same as browse_directory does),
            # because _resolve_repo_path is git-centric and fails for non-git repos
            # like cidx-meta which is a generated folder with .md files.
            registry = get_server_global_registry(golden_repos_dir)
            global_repos = registry.list_global_repos()
            repo_entry = next(
                (r for r in global_repos if r["alias_name"] == repository_alias), None
            )
            if not repo_entry:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Global repository '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                return _mcp_response(error_envelope)
            from code_indexer.global_repos.alias_manager import AliasManager

            alias_manager = AliasManager(str(Path(golden_repos_dir) / "aliases"))
            repo_path = alias_manager.read_alias(repository_alias)
            if not repo_path:
                available_repos = _get_available_repos()
                error_envelope = _error_with_suggestions(
                    error_msg=f"Alias for '{repository_alias}' not found",
                    attempted_value=repository_alias,
                    available_values=available_repos,
                )
                return _mcp_response(error_envelope)
        else:
            repo_path = _resolve_repo_path(repository_alias, golden_repos_dir)
            if repo_path is None:
                return _mcp_response(
                    {
                        "success": False,
                        "error": f"Repository '{repository_alias}' not found",
                    }
                )

        # Create service and generate tree
        service = DirectoryExplorerService(Path(repo_path))
        result = service.generate_tree(
            path=args.get("path"),
            max_depth=_coerce_int(args.get("max_depth"), 3),
            max_files_per_dir=_coerce_int(args.get("max_files_per_dir"), 50),
            include_patterns=args.get("include_patterns"),
            exclude_patterns=args.get("exclude_patterns"),
            show_stats=args.get("show_stats", False),
            include_hidden=args.get("include_hidden", False),
        )

        # Convert TreeNode to dict recursively
        def tree_node_to_dict(node):
            result_dict = {
                "name": node.name,
                "path": node.path,
                "is_directory": node.is_directory,
                "truncated": node.truncated,
                "hidden_count": node.hidden_count,
            }
            if node.children is not None:
                result_dict["children"] = [tree_node_to_dict(c) for c in node.children]
            else:
                result_dict["children"] = None
            return result_dict

        return _mcp_response(
            {
                "success": True,
                "tree_string": result.tree_string,
                "root": tree_node_to_dict(result.root),
                "total_directories": result.total_directories,
                "total_files": result.total_files,
                "max_depth_reached": result.max_depth_reached,
                "root_path": result.root_path,
            }
        )

    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error in directory_tree: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Update handler registry with directory tree tool (Story #557)
HANDLER_REGISTRY["directory_tree"] = handle_directory_tree


def handle_authenticate(
    args: Dict[str, Any], http_request, http_response
) -> Dict[str, Any]:
    """
    Handler for authenticate tool - validates API key and sets JWT cookie.

    This handler has a special signature (Request, Response) because it needs
    to set cookies in the HTTP response.
    """
    from code_indexer.server.auth.dependencies import jwt_manager, user_manager

    # Lazy import to avoid module import side effects during startup
    from code_indexer.server.auth.token_bucket import rate_limiter
    import math

    username = args.get("username")
    api_key = args.get("api_key")

    if not username or not api_key:
        return _mcp_response({"success": False, "error": "Missing username or api_key"})
    # Rate limit check BEFORE validating credentials
    allowed, retry_after = rate_limiter.consume(username)
    if not allowed:
        retry_after_int = int(math.ceil(retry_after))
        return _mcp_response(
            {
                "success": False,
                "error": f"Rate limit exceeded. Try again in {retry_after_int} seconds",
                "retry_after": retry_after_int,
            }
        )

    # Validate API key
    user = user_manager.validate_user_api_key(username, api_key)
    if not user:
        return _mcp_response({"success": False, "error": "Invalid credentials"})

    # Successful authentication should refund the consumed token
    rate_limiter.refund(username)

    # Create JWT token
    token = jwt_manager.create_token(
        {
            "username": user.username,
            "role": user.role.value,
            "created_at": user.created_at.isoformat(),
        }
    )

    # Set JWT as HttpOnly cookie
    http_response.set_cookie(
        key="cidx_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=jwt_manager.token_expiration_minutes * 60,
    )

    return _mcp_response(
        {
            "success": True,
            "message": "Authentication successful",
            "username": user.username,
            "role": user.role.value,
        }
    )


# Register the handler
HANDLER_REGISTRY["authenticate"] = handle_authenticate


# SSH Key Management Handlers (Story #572)
# SSH Key Manager singleton
_ssh_key_manager: SSHKeyManager = None


def get_ssh_key_manager() -> SSHKeyManager:
    """Get or create the SSH key manager instance with SQLite backend (Story #702)."""
    global _ssh_key_manager
    if _ssh_key_manager is None:
        from ..services.config_service import get_config_service

        config_service = get_config_service()
        server_dir = config_service.config_manager.server_dir
        db_path = server_dir / "data" / "cidx_server.db"
        metadata_dir = server_dir / "data" / "ssh_keys"

        _ssh_key_manager = SSHKeyManager(
            metadata_dir=metadata_dir,
            use_sqlite=True,
            db_path=db_path,
        )
    return _ssh_key_manager


def handle_ssh_key_create(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Create a new SSH key pair.

    Args:
        args: Dict with name, key_type (optional), email (optional), description (optional)
        user: Authenticated user

    Returns:
        Dict with success status and public key
    """
    name = args.get("name")
    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )

    key_type = args.get("key_type", "ed25519")
    email = args.get("email")
    description = args.get("description")

    manager = get_ssh_key_manager()

    try:
        metadata = manager.create_key(
            name=name,
            key_type=key_type,
            email=email,
            description=description,
        )

        return _mcp_response(
            {
                "success": True,
                "name": metadata.name,
                "fingerprint": metadata.fingerprint,
                "key_type": metadata.key_type,
                "public_key": metadata.public_key,
                "email": metadata.email,
                "description": metadata.description,
            }
        )

    except InvalidKeyNameError as e:
        return _mcp_response({"success": False, "error": f"Invalid key name: {str(e)}"})
    except KeyAlreadyExistsError as e:
        return _mcp_response(
            {"success": False, "error": f"Key already exists: {str(e)}"}
        )
    except Exception as e:
        logger.exception(
            f"Error creating SSH key: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["cidx_ssh_key_create"] = handle_ssh_key_create


def handle_ssh_key_list(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    List all managed and unmanaged SSH keys.

    Args:
        args: Empty dict (no parameters needed)
        user: Authenticated user

    Returns:
        Dict with managed and unmanaged key lists
    """
    manager = get_ssh_key_manager()

    try:
        result = manager.list_keys()

        managed = [
            {
                "name": k.name,
                "fingerprint": k.fingerprint,
                "key_type": k.key_type,
                "hosts": k.hosts,
                "email": k.email,
                "description": k.description,
                "is_imported": k.is_imported,
            }
            for k in result.managed
        ]

        unmanaged = [
            {
                "name": k.name,
                "fingerprint": k.fingerprint,
                "private_path": str(k.private_path),
            }
            for k in result.unmanaged
        ]

        return _mcp_response(
            {
                "success": True,
                "managed": managed,
                "unmanaged": unmanaged,
            }
        )

    except Exception as e:
        logger.exception(
            f"Error listing SSH keys: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["cidx_ssh_key_list"] = handle_ssh_key_list


def handle_ssh_key_delete(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Delete an SSH key.

    Args:
        args: Dict with name
        user: Authenticated user

    Returns:
        Dict with success status
    """
    name = args.get("name")
    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )

    manager = get_ssh_key_manager()

    try:
        manager.delete_key(name)
        return _mcp_response(
            {
                "success": True,
                "message": f"Key '{name}' deleted",
            }
        )

    except Exception as e:
        logger.exception(
            f"Error deleting SSH key: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["cidx_ssh_key_delete"] = handle_ssh_key_delete


def handle_ssh_key_show_public(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Get the public key content for copy/paste.

    Args:
        args: Dict with name
        user: Authenticated user

    Returns:
        Dict with public key content
    """
    name = args.get("name")
    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )

    manager = get_ssh_key_manager()

    try:
        public_key = manager.get_public_key(name)
        return _mcp_response(
            {
                "success": True,
                "name": name,
                "public_key": public_key,
            }
        )

    except KeyNotFoundError:
        return _mcp_response({"success": False, "error": f"Key not found: {name}"})
    except Exception as e:
        logger.exception(
            f"Error getting public key: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["cidx_ssh_key_show_public"] = handle_ssh_key_show_public


def handle_ssh_key_assign_host(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Assign a host to an SSH key.

    Args:
        args: Dict with name and hostname
        user: Authenticated user

    Returns:
        Dict with updated key information
    """
    name = args.get("name")
    hostname = args.get("hostname")

    if not name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: name"}
        )
    if not hostname:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: hostname"}
        )

    force = args.get("force", False)

    manager = get_ssh_key_manager()

    try:
        metadata = manager.assign_key_to_host(
            key_name=name,
            hostname=hostname,
            force=force,
        )

        return _mcp_response(
            {
                "success": True,
                "name": metadata.name,
                "fingerprint": metadata.fingerprint,
                "key_type": metadata.key_type,
                "hosts": metadata.hosts,
                "email": metadata.email,
                "description": metadata.description,
            }
        )

    except KeyNotFoundError:
        return _mcp_response({"success": False, "error": f"Key not found: {name}"})
    except HostConflictError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Error assigning host to key: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["cidx_ssh_key_assign_host"] = handle_ssh_key_assign_host


# SCIP Call Graph Query Handlers
# Story #40: All SCIP handlers now delegate to SCIPQueryService via _get_scip_query_service()
# The legacy _get_golden_repos_scip_dir() and _find_scip_files() functions have been removed.


def scip_definition(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Find definition locations for a symbol across all indexed projects.

    Args:
        params: Dictionary containing:
            - symbol: Symbol name to search for
            - exact: Optional boolean for exact match
            - project: Optional project filter
            - repository_alias: Optional repository name to filter SCIP indexes
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with definition results
    """
    try:
        symbol = params.get("symbol")
        exact = params.get("exact", False)
        project = params.get("project")
        repository_alias = params.get("repository_alias")

        if not symbol:
            return _mcp_response(
                {"success": False, "error": "symbol parameter is required"}
            )

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        results_dicts = service.find_definition(
            symbol=symbol,
            exact=exact,
            repository_alias=repository_alias,
            username=user.username,
        )

        # Apply project filter if specified (backward compatibility)
        if project:
            results_dicts = [
                r for r in results_dicts if project in r.get("project", "")
            ]

        # Story #685: Apply SCIP payload truncation to context fields
        # Story #50: Truncation functions are now sync
        results_dicts = _apply_scip_payload_truncation(results_dicts)

        return _mcp_response(
            {
                "success": True,
                "symbol": symbol,
                "total_results": len(results_dicts),
                "results": results_dicts,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_definition: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "results": []})


def scip_references(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Find all references to a symbol across all indexed projects.

    Args:
        params: Dictionary containing:
            - symbol: Symbol name to search for
            - limit: Optional maximum number of results (default 100)
            - exact: Optional boolean for exact match
            - project: Optional project filter
            - repository_alias: Optional repository name to filter SCIP indexes
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with reference results
    """
    try:
        symbol = params.get("symbol")
        limit = _coerce_int(params.get("limit"), 100)
        exact = params.get("exact", False)
        project = params.get("project")
        repository_alias = params.get("repository_alias")

        if not symbol:
            return _mcp_response(
                {"success": False, "error": "symbol parameter is required"}
            )

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        results_dicts = service.find_references(
            symbol=symbol,
            limit=limit,
            exact=exact,
            repository_alias=repository_alias,
            username=user.username,
        )

        # Apply project filter if specified (backward compatibility)
        if project:
            results_dicts = [
                r for r in results_dicts if project in r.get("project", "")
            ]

        # Story #685: Apply SCIP payload truncation to context fields
        # Story #50: Truncation functions are now sync
        results_dicts = _apply_scip_payload_truncation(results_dicts)

        return _mcp_response(
            {
                "success": True,
                "symbol": symbol,
                "total_results": len(results_dicts),
                "results": results_dicts,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_references: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "results": []})


def scip_dependencies(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get dependencies for a symbol across all indexed projects.

    Args:
        params: Dictionary containing:
            - symbol: Symbol name to search for
            - depth: Optional depth limit (default 1)
            - exact: Optional boolean for exact match
            - project: Optional project filter
            - repository_alias: Optional repository name to filter SCIP indexes
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with dependency results
    """
    try:
        symbol = params.get("symbol")
        depth = _coerce_int(params.get("depth"), 1)
        exact = params.get("exact", False)
        project = params.get("project")
        repository_alias = params.get("repository_alias")

        if not symbol:
            return _mcp_response(
                {"success": False, "error": "symbol parameter is required"}
            )

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        results_dicts = service.get_dependencies(
            symbol=symbol,
            depth=depth,
            exact=exact,
            repository_alias=repository_alias,
            username=user.username,
        )

        # Apply project filter if specified (backward compatibility)
        if project:
            results_dicts = [
                r for r in results_dicts if project in r.get("project", "")
            ]

        # Story #685: Apply SCIP payload truncation to context fields
        # Story #50: Truncation functions are now sync
        results_dicts = _apply_scip_payload_truncation(results_dicts)

        return _mcp_response(
            {
                "success": True,
                "symbol": symbol,
                "total_results": len(results_dicts),
                "results": results_dicts,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_dependencies: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "results": []})


def scip_dependents(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get dependents (symbols that depend on target symbol) across all indexed projects.

    Args:
        params: Dictionary containing:
            - symbol: Symbol name to search for
            - depth: Optional depth limit (default 1)
            - exact: Optional boolean for exact match
            - project: Optional project filter
            - repository_alias: Optional repository name to filter SCIP indexes
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with dependent results
    """
    try:
        symbol = params.get("symbol")
        depth = _coerce_int(params.get("depth"), 1)
        exact = params.get("exact", False)
        project = params.get("project")
        repository_alias = params.get("repository_alias")

        if not symbol:
            return _mcp_response(
                {"success": False, "error": "symbol parameter is required"}
            )

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        results_dicts = service.get_dependents(
            symbol=symbol,
            depth=depth,
            exact=exact,
            repository_alias=repository_alias,
            username=user.username,
        )

        # Apply project filter if specified (backward compatibility)
        if project:
            results_dicts = [
                r for r in results_dicts if project in r.get("project", "")
            ]

        # Story #685: Apply SCIP payload truncation to context fields
        # Story #50: Truncation functions are now sync
        results_dicts = _apply_scip_payload_truncation(results_dicts)

        return _mcp_response(
            {
                "success": True,
                "symbol": symbol,
                "total_results": len(results_dicts),
                "results": results_dicts,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_dependents: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "results": []})


def scip_impact(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Analyze impact of changes to a symbol.

    Args:
        params: Dictionary containing:
            - symbol: Symbol name to analyze
            - depth: Optional traversal depth (default 3, max 10)
            - repository_alias: Optional repository name to filter SCIP indexes
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with impact analysis results
    """
    try:
        symbol = params.get("symbol")
        depth = _coerce_int(params.get("depth"), 3)
        repository_alias = params.get("repository_alias")

        if not symbol:
            return _mcp_response(
                {"success": False, "error": "symbol parameter is required"}
            )

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        result = service.analyze_impact(
            symbol=symbol,
            depth=depth,
            repository_alias=repository_alias,
            username=user.username,
        )

        return _mcp_response(
            {
                "success": True,
                **result,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_impact: {e}", extra={"correlation_id": get_correlation_id()}
        )
        return _mcp_response(
            {
                "success": False,
                "error": str(e),
                "affected_symbols": [],
                "affected_files": [],
            }
        )


def scip_callchain(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Find call chains between two symbols.

    Args:
        params: Dictionary containing:
            - from_symbol: Starting symbol
            - to_symbol: Target symbol
            - max_depth: Optional maximum chain length (default 10, max 10)
            - repository_alias: Optional repository name to filter SCIP indexes
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with call chain results
    """
    try:
        from_symbol = params.get("from_symbol")
        to_symbol = params.get("to_symbol")
        max_depth = params.get("max_depth", 10)
        repository_alias = params.get("repository_alias")

        # Validate symbol formats
        from_symbol_error = _validate_symbol_format(from_symbol, "from_symbol")
        if from_symbol_error:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Invalid parameters: {from_symbol_error}",
                    "from_symbol": from_symbol,
                    "to_symbol": to_symbol,
                    "chains": [],
                }
            )

        to_symbol_error = _validate_symbol_format(to_symbol, "to_symbol")
        if to_symbol_error:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Invalid parameters: {to_symbol_error}",
                    "from_symbol": from_symbol,
                    "to_symbol": to_symbol,
                    "chains": [],
                }
            )

        # Validate and clamp max_depth to safe range [1, 10]
        if max_depth < 1:
            max_depth = 1
        elif max_depth > 10:
            max_depth = 10

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        all_chains = service.trace_callchain(
            from_symbol=from_symbol,
            to_symbol=to_symbol,
            max_depth=max_depth,
            limit=100,
            repository_alias=repository_alias,
            username=user.username,
        )

        # Deduplicate chains by path
        unique_chains_map = {}
        for chain in all_chains:
            path_key = tuple(chain.get("path", []))
            if path_key not in unique_chains_map:
                unique_chains_map[path_key] = chain

        unique_chains = list(unique_chains_map.values())

        # Check if any chain reached max depth
        max_depth_reached = any(
            chain.get("length", 0) >= max_depth for chain in unique_chains
        )

        # Sort by length (shortest first)
        unique_chains.sort(key=lambda c: c.get("length", 0))

        # Limit to maximum return size (100 chains)
        MAX_CALL_CHAINS_RETURNED = 100
        truncated = len(unique_chains) > MAX_CALL_CHAINS_RETURNED
        returned_chains = unique_chains[:MAX_CALL_CHAINS_RETURNED]

        # Generate diagnostic message if no chains found
        diagnostic = None
        if len(unique_chains) == 0:
            diagnostic = f"No call chains found from '{from_symbol}' to '{to_symbol}'. "
            diagnostic += "Verify symbol names exist in the codebase. Try using simple class or method names."

        return _mcp_response(
            {
                "success": True,
                "from_symbol": from_symbol,
                "to_symbol": to_symbol,
                "total_chains_found": len(unique_chains),
                "truncated": truncated,
                "max_depth_reached": max_depth_reached,
                # Note: scip_files_searched not available via service API
                "scip_files_searched": 0,
                "repository_filter": repository_alias if repository_alias else "all",
                "chains": returned_chains,
                "diagnostic": diagnostic,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_callchain: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "chains": []})


def scip_context(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get smart context for a symbol.

    Args:
        params: Dictionary containing:
            - symbol: Symbol name to analyze
            - limit: Optional maximum files to return (default 20, max 100)
            - min_score: Optional minimum relevance score (default 0.0, range 0.0-1.0)
            - repository_alias: Optional repository name to filter SCIP indexes
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with smart context results
    """
    try:
        symbol = params.get("symbol")
        limit = _coerce_int(params.get("limit"), 20)
        min_score = _coerce_float(params.get("min_score"), 0.0)
        repository_alias = params.get("repository_alias")

        if not symbol:
            return _mcp_response(
                {"success": False, "error": "symbol parameter is required"}
            )

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        result = service.get_context(
            symbol=symbol,
            limit=limit,
            min_score=min_score,
            repository_alias=repository_alias,
            username=user.username,
        )

        return _mcp_response(
            {
                "success": True,
                **result,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_context: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "files": []})


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
        f for f in dependency_map_dir.glob("*.md")
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
        from .tools import TOOL_REGISTRY
        from .tool_doc_loader import _get_tool_doc_loader

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
        if app_module.golden_repo_manager:
            cidx_meta_path = Path(app_module.golden_repo_manager.golden_repos_dir) / "cidx-meta"
            dependency_map_section = _build_dependency_map_section(cidx_meta_path)
            if dependency_map_section:
                response["dependency_map"] = dependency_map_section

        # Conditionally add Langfuse section
        langfuse_section = _build_langfuse_section(
            config, app_module.golden_repo_manager
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


def trigger_reindex(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Trigger manual re-indexing for activated repository.

    Args:
        params: {
            "repository_alias": str - Repository alias to reindex
            "index_types": List[str] - Index types (semantic, fts, temporal, scip)
            "clear": bool - Rebuild from scratch vs incremental (default: False)
        }
        user: User requesting reindex

    Returns:
        MCP response with job details
    """
    import time
    from datetime import datetime, timezone
    from code_indexer.server.services.activated_repo_index_manager import (
        ActivatedRepoIndexManager,
    )

    start_time = time.time()

    try:
        # Extract parameters
        repo_alias = params.get("repository_alias")
        index_types = params.get("index_types", [])
        clear = params.get("clear", False)

        if not repo_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "repository_alias is required",
                }
            )

        if not index_types:
            return _mcp_response(
                {
                    "success": False,
                    "error": "index_types is required",
                }
            )

        # Create index manager and trigger reindex
        index_manager = ActivatedRepoIndexManager()
        job_id = index_manager.trigger_reindex(
            repo_alias=repo_alias,
            index_types=index_types,
            clear=clear,
            username=user.username,
        )

        # Calculate estimated duration based on index types
        # Rough estimates: semantic/fts/temporal=5min each, scip=2min
        duration_estimates = {
            "semantic": 5,
            "fts": 5,
            "temporal": 5,
            "scip": 2,
        }
        estimated_minutes = sum(duration_estimates.get(t, 5) for t in index_types)

        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.info(
            f"trigger_reindex completed in {elapsed_ms}ms - "
            f"job_id={job_id}, repo={repo_alias}, types={index_types}",
            extra={"correlation_id": get_correlation_id()},
        )

        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "status": "queued",
                "index_types": index_types,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "estimated_duration_minutes": estimated_minutes,
            }
        )

    except ValueError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.warning(
            format_error_log(
                "MCP-GENERAL-060",
                f"trigger_reindex validation error in {elapsed_ms}ms: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": str(e),
            }
        )
    except FileNotFoundError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.warning(
            format_error_log(
                "MCP-GENERAL-061",
                f"trigger_reindex repo not found in {elapsed_ms}ms: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": str(e),
            }
        )
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.exception(
            f"trigger_reindex error in {elapsed_ms}ms: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {
                "success": False,
                "error": str(e),
            }
        )


def get_index_status(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get indexing status for all index types.

    Args:
        params: {
            "repository_alias": str - Repository alias
        }
        user: User requesting status

    Returns:
        MCP response with index status for all types
    """
    import time
    from code_indexer.server.services.activated_repo_index_manager import (
        ActivatedRepoIndexManager,
    )

    start_time = time.time()

    try:
        # Extract parameters
        repo_alias = params.get("repository_alias")

        if not repo_alias:
            return _mcp_response(
                {
                    "success": False,
                    "error": "repository_alias is required",
                }
            )

        # Create index manager and get status
        index_manager = ActivatedRepoIndexManager()
        status_data = index_manager.get_index_status(
            repo_alias=repo_alias,
            username=user.username,
        )

        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.info(
            f"get_index_status completed in {elapsed_ms}ms - repo={repo_alias}",
            extra={"correlation_id": get_correlation_id()},
        )

        # Build response with all index types
        response = {
            "success": True,
            "repository_alias": repo_alias,
        }
        response.update(status_data)

        return _mcp_response(response)

    except FileNotFoundError as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.warning(
            format_error_log(
                "MCP-GENERAL-062",
                f"get_index_status repo not found in {elapsed_ms}ms: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": str(e),
            }
        )
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.exception(
            f"get_index_status error in {elapsed_ms}ms: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {
                "success": False,
                "error": str(e),
            }
        )


# Register the quick_reference handler
HANDLER_REGISTRY["cidx_quick_reference"] = quick_reference

# Register re-indexing handlers
HANDLER_REGISTRY["trigger_reindex"] = trigger_reindex
HANDLER_REGISTRY["get_index_status"] = get_index_status


# =============================================================================
# Git Write Operations MCP Handlers (Story #626)
# =============================================================================


def git_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_status tool - get repository working tree status."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_status(Path(repo_path))
        result["success"] = True
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-063",
                f"git_status failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_status: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_stage(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_stage tool - stage files for commit."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    file_paths = args.get("file_paths")
    if not file_paths:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: file_paths"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_stage(Path(repo_path), file_paths)
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-064",
                f"git_stage failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_stage: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_unstage(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_unstage tool - unstage files."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    file_paths = args.get("file_paths")
    if not file_paths:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: file_paths"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_unstage(Path(repo_path), file_paths)
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-065",
                f"git_unstage failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_unstage: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_commit(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_commit tool - create a git commit."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    message = args.get("message")
    if not message:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: message"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Get user email - try from user object first, fallback to username-based
        user_email = getattr(user, "email", None) or f"{user.username}@cidx.local"
        user_name = args.get("author_name") or user.username

        result = git_operations_service.git_commit(
            Path(repo_path), message, user_email, user_name
        )
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-066",
                f"git_commit failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except ValueError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_commit: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_push(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_push tool - push commits to remote."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        # Bug #639: Call push_to_remote wrapper to trigger migration if needed
        remote = args.get("remote", "origin")
        branch = args.get("branch")
        result = git_operations_service.push_to_remote(
            repo_alias=repository_alias,
            username=user.username,
            remote=remote,
            branch=branch,
        )
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-067",
                f"git_push failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_push: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_pull(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_pull tool - pull updates from remote."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        # Bug #639: Call pull_from_remote wrapper to trigger migration if needed
        remote = args.get("remote", "origin")
        branch = args.get("branch")
        result = git_operations_service.pull_from_remote(
            repo_alias=repository_alias,
            username=user.username,
            remote=remote,
            branch=branch,
        )
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-068",
                f"git_pull failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_pull: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_fetch(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_fetch tool - fetch refs from remote."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        # Bug #639: Call fetch_from_remote wrapper to trigger migration if needed
        remote = args.get("remote", "origin")
        result = git_operations_service.fetch_from_remote(
            repo_alias=repository_alias, username=user.username, remote=remote
        )
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-069",
                f"git_fetch failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_fetch: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_reset(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_reset tool - reset working tree."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    mode = args.get("mode", "mixed")
    target = args.get("target")
    confirmation_token = args.get("confirmation_token")

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_reset(
            Path(repo_path),
            mode=mode,
            target=target,
            confirmation_token=confirmation_token,
        )

        # Handle confirmation token requirement
        if result.get("requires_confirmation"):
            return _mcp_response(
                {
                    "success": False,
                    "confirmation_token_required": {
                        "token": result["token"],
                        "message": f"Hard reset requires confirmation. "
                        f"Call again with confirmation_token='{result['token']}'",
                    },
                }
            )

        return _mcp_response(result)

    except ValueError as e:
        # Token validation failed - generate new token
        token = git_operations_service.generate_confirmation_token("git_reset_hard")
        return _mcp_response(
            {
                "success": False,
                "confirmation_token_required": {
                    "token": token,
                    "message": str(e),
                },
            }
        )
    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-070",
                f"git_reset failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_reset: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_clean(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_clean tool - remove untracked files."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    confirmation_token = args.get("confirmation_token")

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_clean(
            Path(repo_path), confirmation_token=confirmation_token
        )

        # Handle confirmation token requirement
        if result.get("requires_confirmation"):
            return _mcp_response(
                {
                    "success": False,
                    "confirmation_token_required": {
                        "token": result["token"],
                        "message": f"Git clean requires confirmation. "
                        f"Call again with confirmation_token='{result['token']}'",
                    },
                }
            )

        return _mcp_response(result)

    except ValueError as e:
        # Token validation failed - generate new token
        token = git_operations_service.generate_confirmation_token("git_clean")
        return _mcp_response(
            {
                "success": False,
                "confirmation_token_required": {
                    "token": token,
                    "message": str(e),
                },
            }
        )
    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-071",
                f"git_clean failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_clean: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_merge_abort(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_merge_abort tool - abort in-progress merge."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_merge_abort(Path(repo_path))
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-072",
                f"git_merge_abort failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_merge_abort: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_checkout_file(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_checkout_file tool - restore file from HEAD."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    file_path = args.get("file_path")
    if not file_path:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: file_path"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_checkout_file(Path(repo_path), file_path)
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-073",
                f"git_checkout_file failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_checkout_file: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_list(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_list tool - list all branches."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_branch_list(Path(repo_path))
        result["success"] = True
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-074",
                f"git_branch_list failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_list: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_create(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_create tool - create new branch."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    branch_name = args.get("branch_name")
    if not branch_name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: branch_name"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_branch_create(Path(repo_path), branch_name)
        # Map created_branch to branch_name for consistent API
        if "created_branch" in result and "branch_name" not in result:
            result["branch_name"] = result["created_branch"]
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-075",
                f"git_branch_create failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_create: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_switch(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_switch tool - switch to different branch."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    branch_name = args.get("branch_name")
    if not branch_name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: branch_name"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_branch_switch(Path(repo_path), branch_name)
        # Map current_branch to branch_name for consistent API
        if "current_branch" in result and "branch_name" not in result:
            result["branch_name"] = result["current_branch"]
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-076",
                f"git_branch_switch failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_switch: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


def git_branch_delete(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_branch_delete tool - delete branch."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    branch_name = args.get("branch_name")
    if not branch_name:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: branch_name"}
        )

    confirmation_token = args.get("confirmation_token")

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        result = git_operations_service.git_branch_delete(
            Path(repo_path), branch_name, confirmation_token=confirmation_token
        )

        # Handle confirmation token requirement
        if result.get("requires_confirmation"):
            return _mcp_response(
                {
                    "success": False,
                    "confirmation_token_required": {
                        "token": result["token"],
                        "message": f"Branch deletion requires confirmation. "
                        f"Call again with confirmation_token='{result['token']}'",
                    },
                }
            )

        return _mcp_response(result)

    except ValueError as e:
        # Token validation failed - generate new token
        token = git_operations_service.generate_confirmation_token("git_branch_delete")
        return _mcp_response(
            {
                "success": False,
                "confirmation_token_required": {
                    "token": token,
                    "message": str(e),
                },
            }
        )
    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-077",
                f"git_branch_delete failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_branch_delete: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Register Git Write Operations handlers (Batch 1, 2, 3, 4 & 5)
HANDLER_REGISTRY["git_status"] = git_status
HANDLER_REGISTRY["git_stage"] = git_stage
HANDLER_REGISTRY["git_unstage"] = git_unstage
HANDLER_REGISTRY["git_commit"] = git_commit
HANDLER_REGISTRY["git_push"] = git_push
HANDLER_REGISTRY["git_pull"] = git_pull
HANDLER_REGISTRY["git_fetch"] = git_fetch
HANDLER_REGISTRY["git_reset"] = git_reset
HANDLER_REGISTRY["git_clean"] = git_clean
HANDLER_REGISTRY["git_merge_abort"] = git_merge_abort
HANDLER_REGISTRY["git_checkout_file"] = git_checkout_file
HANDLER_REGISTRY["git_branch_list"] = git_branch_list
HANDLER_REGISTRY["git_branch_create"] = git_branch_create
HANDLER_REGISTRY["git_branch_switch"] = git_branch_switch
HANDLER_REGISTRY["git_branch_delete"] = git_branch_delete


def git_diff(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_diff tool - get diff of working tree changes with pagination."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Story #686: Extract pagination parameters
        file_paths = args.get("file_paths")
        offset = _coerce_int(args.get("offset"), 0)
        limit = _coerce_int(args.get("limit"), 500) if args.get("limit") is not None else None  # None means use default (500)

        result = git_operations_service.git_diff(
            Path(repo_path), file_paths=file_paths, offset=offset, limit=limit
        )
        result["success"] = True
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-078",
                f"git_diff failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_diff: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["git_diff"] = git_diff


def git_log(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for git_log tool - get commit history with pagination."""
    repository_alias = args.get("repository_alias")
    if not repository_alias:
        return _mcp_response(
            {"success": False, "error": "Missing required parameter: repository_alias"}
        )

    try:
        repo_path, error_msg = _resolve_git_repo_path(repository_alias, user.username)
        if error_msg is not None:
            return _mcp_response({"success": False, "error": error_msg})

        # Story #686: Updated default limit to 50, added offset parameter
        limit = _coerce_int(args.get("limit"), 50)
        offset = _coerce_int(args.get("offset"), 0)
        since_date = args.get("since_date")
        result = git_operations_service.git_log(
            Path(repo_path), limit=limit, offset=offset, since_date=since_date
        )
        result["success"] = True
        return _mcp_response(result)

    except GitCommandError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-079",
                f"git_log failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error_type": "GitCommandError",
                "error": str(e),
                "stderr": e.stderr,
                "command": e.command,
            }
        )
    except FileNotFoundError as e:
        return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.exception(
            f"Unexpected error in git_log: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["git_log"] = git_log


# =============================================================================
# Meta Tools Handlers (Batch 6: first_time_user_guide, get_tool_categories)
# =============================================================================


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


HANDLER_REGISTRY["first_time_user_guide"] = first_time_user_guide


def get_tool_categories(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Handler for get_tool_categories tool - returns tools organized by category.

    Uses ToolDocLoader singleton to build categories from markdown documentation
    files without per-call disk I/O (Story #222 code review Finding 1).
    """
    from .tool_doc_loader import _get_tool_doc_loader

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


HANDLER_REGISTRY["get_tool_categories"] = get_tool_categories


# =============================================================================
# ADMIN LOG MANAGEMENT TOOLS
# =============================================================================


def handle_admin_logs_query(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Query operational logs with pagination and filtering.

    Requires admin role. Returns logs from SQLite database with filters for search,
    level, correlation_id, and pagination controls.

    Args:
        args: Query parameters (page, page_size, search, level, sort_order)
        user: Authenticated user (must be admin)

    Returns:
        MCP-compliant response with logs array and pagination metadata
    """
    # Permission check: admin only
    if user.role != UserRole.ADMIN:
        return _mcp_response(
            {
                "success": False,
                "error": "Permission denied. Admin role required to query logs.",
            }
        )

    # Get log database path from app.state
    log_db_path = getattr(app_module.app.state, "log_db_path", None)
    if not log_db_path:
        return _mcp_response({"success": False, "error": "Log database not configured"})

    # Initialize service
    from code_indexer.server.services.log_aggregator_service import LogAggregatorService

    service = LogAggregatorService(log_db_path)

    # Extract parameters
    page = args.get("page", 1)
    page_size = args.get("page_size", 50)
    sort_order = args.get("sort_order", "desc")
    search = args.get("search")
    level = args.get("level")
    correlation_id = args.get("correlation_id")

    # Parse level (comma-separated string to list)
    levels = None
    if level:
        levels = [lv.strip() for lv in level.split(",")]

    # Query logs
    result = service.query(
        page=page,
        page_size=page_size,
        sort_order=sort_order,
        levels=levels,
        correlation_id=correlation_id,
        search=search,
    )

    return _mcp_response(
        {"success": True, "logs": result["logs"], "pagination": result["pagination"]}
    )


def admin_logs_export(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Export operational logs in JSON or CSV format.

    Requires admin role. Returns ALL logs matching filter criteria (no pagination)
    formatted as JSON or CSV for offline analysis or external tool import.

    Args:
        args: Export parameters (format, search, level, correlation_id)
        user: Authenticated user (must be admin)

    Returns:
        MCP-compliant response with format, count, data, and filters metadata
    """
    # Permission check: admin only
    if user.role != UserRole.ADMIN:
        return _mcp_response(
            {
                "success": False,
                "error": "Permission denied. Admin role required to export logs.",
            }
        )

    # Get log database path from app.state
    log_db_path = getattr(app_module.app.state, "log_db_path", None)
    if not log_db_path:
        return _mcp_response({"success": False, "error": "Log database not configured"})

    # Initialize services
    from code_indexer.server.services.log_aggregator_service import LogAggregatorService
    from code_indexer.server.services.log_export_formatter import LogExportFormatter

    service = LogAggregatorService(log_db_path)
    formatter = LogExportFormatter()

    # Extract parameters
    export_format = args.get("format", "json")
    search = args.get("search")
    level = args.get("level")
    correlation_id = args.get("correlation_id")

    # Validate format
    if export_format not in ["json", "csv"]:
        return _mcp_response(
            {
                "success": False,
                "error": f"Invalid format '{export_format}'. Must be 'json' or 'csv'.",
            }
        )

    # Parse level (comma-separated string to list)
    levels = None
    if level:
        levels = [lv.strip() for lv in level.split(",")]

    # Query ALL logs matching filters (no pagination)
    logs = service.query_all(
        levels=levels, correlation_id=correlation_id, search=search
    )

    # Format output
    filters = {"search": search, "level": level, "correlation_id": correlation_id}

    if export_format == "json":
        data = formatter.to_json(logs, filters)
    else:  # csv
        data = formatter.to_csv(logs)

    return _mcp_response(
        {
            "success": True,
            "format": export_format,
            "count": len(logs),
            "data": data,
            "filters": filters,
        }
    )


HANDLER_REGISTRY["admin_logs_query"] = handle_admin_logs_query
HANDLER_REGISTRY["admin_logs_export"] = admin_logs_export


def get_scip_audit_log(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Get SCIP dependency installation audit log with filtering.

    Admin-only endpoint for querying SCIP dependency installation history.
    Supports filtering by job_id, repo_alias, project_language, and project_build_system.

    Args:
        params: Query parameters (job_id, repo_alias, project_language,
                project_build_system, limit, offset)
        user: Authenticated user (must be admin)

    Returns:
        MCP response with audit records, total count, and applied filters
    """
    try:
        # Check admin permission
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin access required for audit logs.",
                }
            )

        # Extract filter parameters
        job_id = params.get("job_id")
        repo_alias = params.get("repo_alias")
        project_language = params.get("project_language")
        project_build_system = params.get("project_build_system")

        # Extract and validate pagination parameters
        limit = params.get("limit", 100)
        offset = params.get("offset", 0)

        # Convert and validate pagination params
        try:
            limit = int(limit)
            offset = int(offset)
            # Ensure positive and bounded limit (1-1000)
            limit = max(1, min(limit, 1000))
            # Ensure non-negative offset
            offset = max(0, offset)
        except (ValueError, TypeError):
            # Use defaults if conversion fails
            limit = 100
            offset = 0

        # Query audit repository
        records, total = scip_audit_repository.query_audit_records(
            job_id=job_id,
            repo_alias=repo_alias,
            project_language=project_language,
            project_build_system=project_build_system,
            limit=limit,
            offset=offset,
        )

        # Build filters dict (echo applied filters in response)
        filters = {}
        if job_id:
            filters["job_id"] = job_id
        if repo_alias:
            filters["repo_alias"] = repo_alias
        if project_language:
            filters["project_language"] = project_language
        if project_build_system:
            filters["project_build_system"] = project_build_system

        return _mcp_response(
            {
                "success": True,
                "records": records,
                "total": total,
                "filters": filters,
            }
        )

    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-080",
                f"Error retrieving SCIP audit log: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["get_scip_audit_log"] = get_scip_audit_log


# Register SCIP handlers
HANDLER_REGISTRY["scip_definition"] = scip_definition
HANDLER_REGISTRY["scip_references"] = scip_references
HANDLER_REGISTRY["scip_dependencies"] = scip_dependencies
HANDLER_REGISTRY["scip_dependents"] = scip_dependents
HANDLER_REGISTRY["scip_impact"] = scip_impact
HANDLER_REGISTRY["scip_callchain"] = scip_callchain
HANDLER_REGISTRY["scip_context"] = scip_context


# Story #633: GitHub Actions Monitoring Handlers
async def handle_gh_actions_list_runs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_list_runs tool.

    Lists workflow runs for a repository with optional filtering by branch and status.
    Implements AC1-AC3 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - branch (str, optional): Filter by branch name
            - status (str, optional): Filter by run status
            - limit (int, optional): Maximum runs to return (default 10)
        user: Authenticated user

    Returns:
        MCP response with workflow runs list
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Extract optional parameters
        branch = args.get("branch")
        status = args.get("status")
        limit = _coerce_int(args.get("limit"), 10)

        # Create client and list runs
        client = GitHubActionsClient(token)
        runs = await client.list_runs(
            repository=repository, branch=branch, status=status
        )

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "runs": runs,
                "count": len(runs),
                "filters": {
                    "branch": branch,
                    "status": status,
                    "limit": limit,
                },
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-081",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-082",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_list_runs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_get_run(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Handler for gh_actions_get_run tool.

    Gets detailed information about a specific workflow run.
    Implements AC4 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID
        user: Authenticated user

    Returns:
        MCP response with detailed run information
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get run details
        client = GitHubActionsClient(token)
        run_info = await client.get_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "run": run_info,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-083",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-084",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_get_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_search_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_search_logs tool.

    Searches workflow run logs using ripgrep pattern matching.
    Implements AC5 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID
            - pattern (str): Search pattern (regex)
            - context_lines (int, optional): Context lines around matches (default 2)
        user: Authenticated user

    Returns:
        MCP response with search matches
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        pattern = args.get("pattern")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )
        if not pattern:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pattern"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and search logs
        client = GitHubActionsClient(token)
        matches = await client.search_logs(
            repository=repository,
            run_id=run_id,
            pattern=pattern,
        )

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "pattern": pattern,
                "matches": matches,
                "match_count": len(matches),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-085",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-086",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_search_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_get_job_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_get_job_logs tool.

    Gets complete logs for a specific job within a workflow run.
    Implements AC6 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - job_id (int): Job ID
        user: Authenticated user

    Returns:
        MCP response with job logs
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        job_id = args.get("job_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get job logs
        client = GitHubActionsClient(token)
        logs = await client.get_job_logs(repository=repository, job_id=job_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "job_id": job_id,
                "logs": logs,
                "log_length": len(logs),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-087",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-088",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_get_job_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_retry_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_retry_run tool.

    Retries a failed workflow run.
    Implements AC7 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID to retry
        user: Authenticated user

    Returns:
        MCP response confirming retry operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and retry run
        client = GitHubActionsClient(token)
        result = await client.retry_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "message": "Workflow run retry triggered successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-089",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-090",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_retry_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gh_actions_cancel_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gh_actions_cancel_run tool.

    Cancels a running or queued workflow run.
    Implements AC8 of Story #633.

    Args:
        args: Tool arguments containing:
            - repository (str): Repository in "owner/repo" format
            - run_id (int): Workflow run ID to cancel
        user: Authenticated user

    Returns:
        MCP response confirming cancellation operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        repository = args.get("repository")
        run_id = args.get("run_id")
        if not repository:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repository"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GH_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and cancel run
        client = GitHubActionsClient(token)
        result = await client.cancel_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "repository": repository,
                "run_id": run_id,
                "message": "Workflow run cancelled successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-091",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-092",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{repository}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gh_actions_cancel_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# gh_actions_* HANDLER_REGISTRY entries removed (Story #222 TODO 5).
# Handler functions preserved below for REST routes in cicd.py.


# ============================================================================
# GitLab CI Handlers (Story #634)
# ============================================================================


async def handle_gitlab_ci_list_pipelines(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_list_pipelines tool.

    Lists pipelines for a GitLab project with optional filtering by ref and status.
    Implements AC1-AC3 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path (e.g., "gitlab-org/gitlab")
            - ref (str, optional): Filter by branch/tag name
            - status (str, optional): Filter by pipeline status
            - limit (int, optional): Maximum pipelines to return (default 10)
        user: Authenticated user

    Returns:
        MCP response with pipelines list
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )

        # Resolve GitLab token
        token = TokenAuthenticator.resolve_token("gitlab")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Extract optional parameters
        ref = args.get("ref")
        status = args.get("status")
        limit = _coerce_int(args.get("limit"), 10)

        # Create client and list pipelines (CRITICAL: keyword)
        client = GitLabCIClient(token)
        pipelines = await client.list_pipelines(
            project_id=project_id, ref=ref, status=status
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipelines": pipelines,
                "count": len(pipelines),
                "filters": {
                    "ref": ref,
                    "status": status,
                    "limit": limit,
                },
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-093",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-094",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_list_pipelines: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_get_pipeline(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_get_pipeline tool.

    Gets detailed information about a specific pipeline including jobs.
    Implements AC4 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID
        user: Authenticated user

    Returns:
        MCP response with detailed pipeline information
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )

        # Resolve GitLab token
        token = TokenAuthenticator.resolve_token("gitlab")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get pipeline details (CRITICAL: keyword)
        client = GitLabCIClient(token)
        pipeline_info = await client.get_pipeline(
            project_id=project_id, pipeline_id=pipeline_id
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "pipeline": pipeline_info,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-095",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-096",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_get_pipeline: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_search_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_search_logs tool.

    Searches pipeline job logs using ripgrep patterns.
    Implements AC5 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID
            - pattern (str): Ripgrep search pattern
            - case_sensitive (bool, optional): Case-sensitive search (default True)
        user: Authenticated user

    Returns:
        MCP response with matching log lines
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        pattern = args.get("pattern")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )
        if not pattern:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pattern"}
            )

        # Resolve GitLab token
        token = TokenAuthenticator.resolve_token("gitlab")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Extract optional parameters
        case_sensitive = args.get("case_sensitive", True)

        # Create client and search logs (CRITICAL: keyword)
        client = GitLabCIClient(token)
        matches = await client.search_logs(
            project_id=project_id,
            pipeline_id=pipeline_id,
            pattern=pattern,
            case_sensitive=case_sensitive,
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "pattern": pattern,
                "matches": matches,
                "count": len(matches),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-097",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-098",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_search_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Register first batch of GitLab CI handlers
HANDLER_REGISTRY["gitlab_ci_list_pipelines"] = handle_gitlab_ci_list_pipelines
HANDLER_REGISTRY["gitlab_ci_get_pipeline"] = handle_gitlab_ci_get_pipeline
HANDLER_REGISTRY["gitlab_ci_search_logs"] = handle_gitlab_ci_search_logs


async def handle_gitlab_ci_get_job_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_get_job_logs tool.

    Gets complete logs for a specific job.
    Implements AC6 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - job_id (int): Job ID
        user: Authenticated user

    Returns:
        MCP response with complete job logs
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        job_id = args.get("job_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # Resolve GitLab token
        token = TokenAuthenticator.resolve_token("gitlab")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and get job logs (CRITICAL: keyword)
        client = GitLabCIClient(token)
        logs = await client.get_job_logs(project_id=project_id, job_id=job_id)

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "job_id": job_id,
                "logs": logs,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-099",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-100",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_get_job_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_retry_pipeline(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_retry_pipeline tool.

    Retries a failed pipeline.
    Implements AC7 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID to retry
        user: Authenticated user

    Returns:
        MCP response confirming retry operation
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )

        # Resolve GitLab token
        token = TokenAuthenticator.resolve_token("gitlab")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and retry pipeline (CRITICAL: keyword)
        client = GitLabCIClient(token)
        result = await client.retry_pipeline(
            project_id=project_id, pipeline_id=pipeline_id
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "message": "Pipeline retried successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-101",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-102",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_retry_pipeline: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_gitlab_ci_cancel_pipeline(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for gitlab_ci_cancel_pipeline tool.

    Cancels a running or pending pipeline.
    Implements AC8 of Story #634.

    Args:
        args: Tool arguments containing:
            - project_id (str): GitLab project ID or path
            - pipeline_id (int): Pipeline ID to cancel
        user: Authenticated user

    Returns:
        MCP response confirming cancellation operation
    """
    from code_indexer.server.clients.gitlab_ci_client import (
        GitLabCIClient,
        GitLabAuthenticationError,
        GitLabProjectNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        project_id = args.get("project_id")
        pipeline_id = args.get("pipeline_id")
        if not project_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: project_id"}
            )
        if not pipeline_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: pipeline_id"}
            )

        # Resolve GitLab token
        token = TokenAuthenticator.resolve_token("gitlab")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitLab token not found. Set GITLAB_TOKEN environment variable or configure token storage.",
                }
            )

        # Create client and cancel pipeline (CRITICAL: keyword)
        client = GitLabCIClient(token)
        result = await client.cancel_pipeline(
            project_id=project_id, pipeline_id=pipeline_id
        )

        return _mcp_response(
            {
                "success": True,
                "project_id": project_id,
                "pipeline_id": pipeline_id,
                "message": "Pipeline cancelled successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitLabAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-103",
                f"GitLab authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitLab authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitLabProjectNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-104",
                f"GitLab project not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Project '{project_id}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in gitlab_ci_cancel_pipeline: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Register remaining GitLab CI handlers
HANDLER_REGISTRY["gitlab_ci_get_job_logs"] = handle_gitlab_ci_get_job_logs
HANDLER_REGISTRY["gitlab_ci_retry_pipeline"] = handle_gitlab_ci_retry_pipeline
HANDLER_REGISTRY["gitlab_ci_cancel_pipeline"] = handle_gitlab_ci_cancel_pipeline


# =============================================================================
# GITHUB ACTIONS HANDLERS (Story #633)
# =============================================================================


async def handle_github_actions_list_runs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_list_runs tool.

    Lists workflow runs for a GitHub repository with optional filtering.
    Implements AC1-AC3 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - workflow_id (str, optional): Filter by workflow ID or filename
            - status (str, optional): Filter by run status
            - branch (str, optional): Filter by branch name
            - limit (int, optional): Maximum runs to return (default 20)
        user: Authenticated user

    Returns:
        MCP response with workflow runs list
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Extract optional parameters
        workflow_id = args.get("workflow_id")
        status = args.get("status")
        branch = args.get("branch")
        limit = _coerce_int(args.get("limit"), 20)

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and list runs (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        runs = await client.list_runs(
            repository=repository, branch=branch, status=status
        )

        # Apply limit to results
        if limit:
            runs = runs[:limit]

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "runs": runs,
                "count": len(runs),
                "filters": {
                    "workflow_id": workflow_id,
                    "status": status,
                    "branch": branch,
                    "limit": limit,
                },
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-105",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-106",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_list_runs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_get_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_get_run tool.

    Gets detailed information for a specific workflow run.
    Implements AC4 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID
        user: Authenticated user

    Returns:
        MCP response with detailed run information
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and get run details (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        run_details = await client.get_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run": run_details,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-107",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-108",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_get_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_search_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_search_logs tool.

    Searches workflow run logs for a pattern.
    Implements AC5 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID
            - query (str): Search query string
        user: Authenticated user

    Returns:
        MCP response with matching log lines
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        query = args.get("query")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )
        if not query:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: query"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and search logs (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        matches = await client.search_logs(
            repository=repository, run_id=run_id, pattern=query
        )

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run_id": run_id,
                "query": query,
                "matches": matches,
                "count": len(matches),
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-109",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-110",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_search_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_get_job_logs(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_get_job_logs tool.

    Gets full log output for a specific job.
    Implements AC6 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - job_id (int): Job ID
        user: Authenticated user

    Returns:
        MCP response with full job logs
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        job_id = args.get("job_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and get job logs (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        logs = await client.get_job_logs(repository=repository, job_id=job_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "job_id": job_id,
                "logs": logs,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-111",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-112",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_get_job_logs: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_retry_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_retry_run tool.

    Retries a failed workflow run.
    Implements AC7 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID to retry
        user: Authenticated user

    Returns:
        MCP response confirming retry operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and retry run (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        result = await client.retry_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run_id": run_id,
                "message": "Workflow run retry triggered successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-113",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-114",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_retry_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


async def handle_github_actions_cancel_run(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Handler for github_actions_cancel_run tool.

    Cancels a running workflow.
    Implements AC8 of Story #633.

    Args:
        args: Tool arguments containing:
            - owner (str): Repository owner
            - repo (str): Repository name
            - run_id (int): Workflow run ID to cancel
        user: Authenticated user

    Returns:
        MCP response confirming cancellation operation
    """
    from code_indexer.server.clients.github_actions_client import (
        GitHubActionsClient,
        GitHubAuthenticationError,
        GitHubRepositoryNotFoundError,
    )
    from code_indexer.server.services.git_state_manager import TokenAuthenticator

    try:
        # Validate required parameters
        owner = args.get("owner")
        repo = args.get("repo")
        run_id = args.get("run_id")
        if not owner:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: owner"}
            )
        if not repo:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo"}
            )
        if not run_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: run_id"}
            )

        # Resolve GitHub token
        token = TokenAuthenticator.resolve_token("github")
        if not token:
            return _mcp_response(
                {
                    "success": False,
                    "error": "GitHub token not found. Set GITHUB_TOKEN environment variable or configure token storage.",
                }
            )

        # Combine owner and repo into repository format
        repository = f"{owner}/{repo}"

        # Create client and cancel run (CRITICAL: keyword)
        client = GitHubActionsClient(token)
        result = await client.cancel_run(repository=repository, run_id=run_id)

        return _mcp_response(
            {
                "success": True,
                "owner": owner,
                "repo": repo,
                "run_id": run_id,
                "message": "Workflow run cancelled successfully",
                "result": result,
                "rate_limit": client.last_rate_limit,
            }
        )

    except GitHubAuthenticationError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-115",
                f"GitHub authentication failed: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "GitHub authentication failed. Check token validity.",
                "details": str(e),
            }
        )
    except GitHubRepositoryNotFoundError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-116",
                f"GitHub repository not found: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Repository '{owner}/{repo}' not found or not accessible.",
                "details": str(e),
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in github_actions_cancel_run: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Register GitHub Actions handlers
HANDLER_REGISTRY["github_actions_list_runs"] = handle_github_actions_list_runs
HANDLER_REGISTRY["github_actions_get_run"] = handle_github_actions_get_run
HANDLER_REGISTRY["github_actions_search_logs"] = handle_github_actions_search_logs
HANDLER_REGISTRY["github_actions_get_job_logs"] = handle_github_actions_get_job_logs
HANDLER_REGISTRY["github_actions_retry_run"] = handle_github_actions_retry_run
HANDLER_REGISTRY["github_actions_cancel_run"] = handle_github_actions_cancel_run


# ============================================================================
# Story #679: Semantic Search with Payload Control - Cache Retrieval Handler
# ============================================================================


def handle_get_cached_content(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Handler for get_cached_content tool.

    Retrieves cached content by handle with pagination support.
    Implements AC5 of Story #679.

    Args:
        args: Tool arguments containing:
            - handle (str): UUID4 cache handle from search results
            - page (int, optional): Page number (0-indexed, default 0)
        user: Authenticated user

    Returns:
        MCP response with content and pagination info
    """
    from code_indexer.server.cache.payload_cache import CacheNotFoundError

    handle = args.get("handle")
    page = args.get("page", 0)

    if not handle:
        return _mcp_response(
            {
                "success": False,
                "error": "Missing required parameter: handle",
            }
        )

    # Get payload_cache from app.state
    payload_cache = getattr(app_module.app.state, "payload_cache", None)

    if payload_cache is None:
        return _mcp_response(
            {
                "success": False,
                "error": "Cache service not available",
            }
        )

    try:
        result = payload_cache.retrieve(handle, page=page)
        return _mcp_response(
            {
                "success": True,
                "content": result.content,
                "page": result.page,
                "total_pages": result.total_pages,
                "has_more": result.has_more,
            }
        )
    except CacheNotFoundError as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-117",
                f"Cache handle not found or expired: {handle}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {
                "success": False,
                "error": "cache_expired",
                "message": str(e),
                "handle": handle,
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in get_cached_content: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Register cache retrieval handler
HANDLER_REGISTRY["get_cached_content"] = handle_get_cached_content


# =============================================================================
# Story #722: Session Impersonation for Delegated Queries
# =============================================================================


def handle_set_session_impersonation(
    args: Dict[str, Any], user: User, session_state=None
) -> Dict[str, Any]:
    """
    Handler for set_session_impersonation tool.

    Allows ADMIN users to set or clear session impersonation.
    When impersonating, all subsequent tool calls use the target user's permissions.

    Args:
        args: Tool arguments containing optional 'username' to impersonate
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for managing impersonation

    Returns:
        dict with status and impersonating username (or null if cleared)
    """
    from code_indexer.server.auth.user_manager import UserRole
    from code_indexer.server.auth.audit_logger import password_audit_logger

    username = args.get("username")

    # Check if user is ADMIN
    if user.role != UserRole.ADMIN:
        password_audit_logger.log_impersonation_denied(
            actor_username=user.username,
            target_username=username or "(clear)",
            reason="Impersonation requires ADMIN role",
            session_id=session_state.session_id if session_state else "unknown",
            ip_address="unknown",
        )
        return _mcp_response(
            {"status": "error", "error": "Impersonation requires ADMIN role"}
        )

    # Handle clearing impersonation
    if username is None:
        if session_state and session_state.is_impersonating:
            previous_target = session_state.impersonated_user.username
            session_state.clear_impersonation()
            password_audit_logger.log_impersonation_cleared(
                actor_username=user.username,
                previous_target=previous_target,
                session_id=session_state.session_id,
                ip_address="unknown",
            )
        return _mcp_response({"status": "ok", "impersonating": None})

    # Look up target user and set impersonation
    try:
        # Bug fix: Use app_module.user_manager (properly configured with SQLite backend)
        # instead of creating new UserManager() which defaults to JSON file storage
        target_user = app_module.user_manager.get_user(username)

        if target_user is None:
            return _mcp_response(
                {"status": "error", "error": f"User not found: {username}"}
            )

        if session_state:
            session_state.set_impersonation(target_user)
            password_audit_logger.log_impersonation_set(
                actor_username=user.username,
                target_username=username,
                session_id=session_state.session_id,
                ip_address="unknown",
            )

        return _mcp_response({"status": "ok", "impersonating": username})

    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-118",
                f"Error in set_session_impersonation: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"status": "error", "error": str(e)})


# Register session impersonation handler
HANDLER_REGISTRY["set_session_impersonation"] = handle_set_session_impersonation


# =============================================================================
# Story #718: Function Discovery for claude.ai Users
# =============================================================================


def _get_delegation_function_repo_path() -> Optional[Path]:
    """
    Get the path to the delegation function repository.

    Returns:
        Path to the function repository, or None if not configured
    """
    from ..services.config_service import get_config_service

    try:
        config_service = get_config_service()
        delegation_manager = config_service.get_delegation_manager()
        delegation_config = delegation_manager.load_config()

        if delegation_config is None or not delegation_config.is_configured:
            return None

        # Get the function repo alias from config
        function_repo_alias = delegation_config.function_repo_alias
        if not function_repo_alias:
            return None

        # Get the actual path from golden repo manager
        golden_repo_manager = getattr(app_module, "golden_repo_manager", None)
        if not golden_repo_manager:
            logger.warning(
                format_error_log("MCP-GENERAL-119", "Golden repo manager not available")
            )
            return None

        # Try to get the repo path
        try:
            repo_path = golden_repo_manager.get_actual_repo_path(function_repo_alias)
            return Path(repo_path) if repo_path else None
        except Exception as e:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-120",
                    f"Function repository '{function_repo_alias}' not found: {e}",
                )
            )
            return None

    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-121", f"Error getting delegation function repo path: {e}"
            )
        )
        return None


def _get_user_groups(user: User) -> set:
    """
    Get the groups the user belongs to.

    Args:
        user: The user to get groups for

    Returns:
        Set of group names the user belongs to
    """
    try:
        group_manager = getattr(app_module.app.state, "group_manager", None)
        if not group_manager:
            logger.warning(
                format_error_log("MCP-GENERAL-122", "Group manager not available")
            )
            return set()

        group = group_manager.get_user_group(user.username)
        if group:
            return {group.name}
        return set()

    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-123", f"Error getting user groups for {user.username}: {e}"
            )
        )
        return set()


def handle_list_delegation_functions(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    List available delegation functions for the current user.

    Functions are filtered based on the effective user's group memberships.
    When impersonation is active, the impersonated user's groups are used.

    Args:
        args: Tool arguments (currently unused)
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for accessing effective user

    Returns:
        MCP response with list of accessible functions
    """
    from ..services.delegation_function_loader import DelegationFunctionLoader

    try:
        # Get the function repository path
        repo_path = _get_delegation_function_repo_path()
        if repo_path is None:
            return _mcp_response(
                {"success": False, "error": "Claude Delegation not configured"}
            )

        # Determine effective user for group lookup (CRITICAL-1 fix)
        # When impersonating, use the impersonated user's groups
        effective_user = user
        if session_state and session_state.is_impersonating:
            effective_user = session_state.effective_user

        # Get effective user's groups
        user_groups = _get_user_groups(effective_user)

        # Load and filter functions
        loader = DelegationFunctionLoader()
        all_functions = loader.load_functions(repo_path)
        accessible_functions = loader.filter_by_groups(all_functions, user_groups)

        # Format response
        functions_data = [
            {
                "name": func.name,
                "description": func.description,
                "parameters": func.parameters,
            }
            for func in accessible_functions
        ]

        return _mcp_response({"success": True, "functions": functions_data})

    except Exception as e:
        logger.exception(
            f"Error in list_delegation_functions: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


# Register delegation functions handler
HANDLER_REGISTRY["list_delegation_functions"] = handle_list_delegation_functions


# =============================================================================
# Story #719: Execute Delegation Function with Async Job
# =============================================================================


def _get_delegation_config():
    """
    Get the Claude Delegation configuration.

    Returns:
        ClaudeDelegationConfig if configured, None otherwise
    """
    from ..services.config_service import get_config_service

    try:
        config_service = get_config_service()
        delegation_manager = config_service.get_delegation_manager()
        return delegation_manager.load_config()
    except Exception as e:
        logger.warning(
            format_error_log("MCP-GENERAL-124", f"Error getting delegation config: {e}")
        )
        return None


def _validate_function_parameters(
    target_function, parameters: Dict[str, Any]
) -> Optional[str]:
    """
    Validate required parameters are present.

    Returns:
        Error message if validation fails, None if valid
    """
    for param in target_function.parameters:
        if param.get("required", False):
            param_name = param.get("name", "")
            if param_name and param_name not in parameters:
                return f"Missing required parameter: {param_name}"
    return None


async def _ensure_repos_registered(
    client, required_repos: List[Dict[str, Any]]
) -> List[str]:
    """
    Ensure required repositories are registered in Claude Server.

    Returns:
        List of repository aliases
    """
    repo_aliases = []
    for repo_def in required_repos:
        # Support both string (alias only) and dict (full repo definition)
        if isinstance(repo_def, str):
            alias = repo_def
            remote = ""
            branch = "main"
        else:
            alias = repo_def.get("alias", "")
            remote = repo_def.get("remote", "")
            branch = repo_def.get("branch", "main")
        if not alias:
            continue
        repo_aliases.append(alias)
        exists = await client.check_repository_exists(alias)
        if not exists and remote:
            # Only register if we have remote URL and repo doesn't exist
            await client.register_repository(alias, remote, branch)
    return repo_aliases


def _get_cidx_callback_base_url() -> Optional[str]:
    """
    Get the base URL for CIDX callback endpoints from delegation config.

    Story #720: Callback-Based Delegation Job Completion

    Returns:
        The CIDX callback URL from delegation config, or None if not configured
    """
    from ..services.config_service import get_config_service

    try:
        config_service = get_config_service()
        delegation_manager = config_service.get_delegation_manager()
        delegation_config = delegation_manager.load_config()

        if delegation_config and delegation_config.cidx_callback_url:
            return delegation_config.cidx_callback_url
        return None
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-125",
                "Failed to get CIDX callback URL from delegation config: %s",
                e,
            )
        )
        return None


async def handle_execute_delegation_function(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    Execute a delegation function by delegating to Claude Server.

    Args:
        args: Tool arguments with function_name, parameters, prompt
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for impersonation

    Returns:
        MCP response with job_id on success or error details
    """
    from ..services.delegation_function_loader import DelegationFunctionLoader
    from ..services.prompt_template_processor import PromptTemplateProcessor
    from ..clients.claude_server_client import ClaudeServerClient, ClaudeServerError

    try:
        # Configuration validation
        repo_path = _get_delegation_function_repo_path()
        delegation_config = _get_delegation_config()

        if (
            repo_path is None
            or delegation_config is None
            or not delegation_config.is_configured
        ):
            return _mcp_response(
                {"success": False, "error": "Claude Delegation not configured"}
            )

        function_name = args.get("function_name", "")
        parameters = args.get("parameters", {})
        user_prompt = args.get("prompt", "")

        # Load and find function
        loader = DelegationFunctionLoader()
        all_functions = loader.load_functions(repo_path)
        target_function = next(
            (f for f in all_functions if f.name == function_name), None
        )

        if target_function is None:
            return _mcp_response(
                {"success": False, "error": f"Function not found: {function_name}"}
            )

        # Access validation
        effective_user = (
            session_state.effective_user
            if session_state and session_state.is_impersonating
            else user
        )
        user_groups = _get_user_groups(effective_user)

        if not (user_groups & set(target_function.allowed_groups)):
            return _mcp_response(
                {"success": False, "error": "Access denied: insufficient permissions"}
            )

        # Parameter validation
        param_error = _validate_function_parameters(target_function, parameters)
        if param_error:
            return _mcp_response({"success": False, "error": param_error})

        # Create client and ensure repos registered
        # Story #732: Use async context manager for proper connection cleanup
        async with ClaudeServerClient(
            base_url=delegation_config.claude_server_url,
            username=delegation_config.claude_server_username,
            password=delegation_config.claude_server_credential,
            skip_ssl_verify=delegation_config.skip_ssl_verify,
        ) as client:
            repo_aliases = await _ensure_repos_registered(
                client, target_function.required_repos
            )

            # Render prompt and create job
            processor = PromptTemplateProcessor()
            impersonation_user = (
                target_function.impersonation_user or effective_user.username
            )
            rendered_prompt = processor.render(
                template=target_function.prompt_template,
                parameters=parameters,
                user_prompt=user_prompt,
                impersonation_user=impersonation_user,
            )

            job_result = await client.create_job(
                prompt=rendered_prompt, repositories=repo_aliases
            )
            # Claude Server returns camelCase "jobId"
            job_id = job_result.get("jobId") or job_result.get("job_id")
            if not job_id:
                return _mcp_response(
                    {"success": False, "error": "Job created but no job_id returned"}
                )

            # Story #720: Register callback URL with Claude Server for completion notification
            callback_base_url = _get_cidx_callback_base_url()
            if callback_base_url:
                callback_url = (
                    f"{callback_base_url.rstrip('/')}/api/delegation/callback/{job_id}"
                )
                try:
                    await client.register_callback(job_id, callback_url)
                    logger.debug(
                        f"Registered callback URL for job {job_id}: {callback_url}"
                    )
                except Exception as callback_err:
                    # Log but don't fail - callback registration is best-effort
                    logger.warning(
                        format_error_log(
                            "MCP-GENERAL-126",
                            f"Failed to register callback for job {job_id}: {callback_err}",
                            extra={"correlation_id": get_correlation_id()},
                        )
                    )

            # Story #720: Register job in tracker for callback-based completion
            from ..services.delegation_job_tracker import DelegationJobTracker

            tracker = DelegationJobTracker.get_instance()
            await tracker.register_job(job_id)

            await client.start_job(job_id)

            return _mcp_response({"success": True, "job_id": job_id})

    except ClaudeServerError as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-127",
                f"Claude Server error: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": f"Claude Server error: {e}"})
    except Exception as e:
        logger.exception(
            f"Error in execute_delegation_function: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["execute_delegation_function"] = handle_execute_delegation_function


async def handle_poll_delegation_job(
    args: Dict[str, Any], user: User, *, session_state=None
) -> Dict[str, Any]:
    """
    Wait for delegation job completion via callback mechanism.

    Story #720: Callback-Based Delegation Job Completion
    Story #50: This handler remains async (justified exception) because
    DelegationJobTracker uses asyncio.Future for callback-based completion.

    Instead of polling Claude Server repeatedly, this waits on a Future
    that gets resolved when Claude Server POSTs the callback to CIDX.

    Args:
        args: Tool arguments with job_id and optional timeout
        user: The authenticated user making the request
        session_state: Optional MCPSessionState for impersonation

    Returns:
        MCP response with result when callback arrives, or timeout/error
    """
    from ..services.delegation_job_tracker import DelegationJobTracker

    job_id = ""
    try:
        # Configuration validation
        delegation_config = _get_delegation_config()

        if delegation_config is None or not delegation_config.is_configured:
            return _mcp_response(
                {"success": False, "error": "Claude Delegation not configured"}
            )

        job_id = args.get("job_id", "")
        if not job_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: job_id"}
            )

        # Story #720: Get timeout_seconds from args (default 45s, below MCP's 60s)
        # Also support legacy "timeout" parameter for backward compatibility
        timeout = args.get("timeout_seconds", args.get("timeout", 45))
        if not isinstance(timeout, (int, float)):
            return _mcp_response(
                {
                    "success": False,
                    "error": "timeout_seconds must be a number (recommended: 5-300)",
                }
            )
        # Minimum 0.01s (for testing), maximum 300s (5 minutes)
        # Recommended range for production: 5-300 seconds
        if timeout < 0.01 or timeout > 300:
            return _mcp_response(
                {
                    "success": False,
                    "error": "timeout_seconds must be between 0.01 and 300",
                }
            )

        # Check if job exists in tracker before waiting
        tracker = DelegationJobTracker.get_instance()
        job_exists = await tracker.has_job(job_id)
        if not job_exists:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Job {job_id} not found or already completed",
                }
            )

        # Wait for callback via DelegationJobTracker
        result = await tracker.wait_for_job(job_id, timeout=timeout)

        if result is None:
            # Timeout - job still exists, caller can try again
            return _mcp_response(
                {
                    "status": "waiting",
                    "message": "Job still running, callback not yet received",
                    "continue_polling": True,
                }
            )

        # Return result based on status from callback
        if result.status == "completed":
            return _mcp_response(
                {
                    "status": "completed",
                    "result": result.output,
                    "continue_polling": False,
                }
            )
        else:
            # Failed or other status
            return _mcp_response(
                {
                    "status": "failed",
                    "error": result.error or result.output,
                    "continue_polling": False,
                }
            )

    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-128",
                f"Error waiting for delegation job {job_id}: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": f"Error waiting for job completion: {str(e)}"}
        )


HANDLER_REGISTRY["poll_delegation_job"] = handle_poll_delegation_job


# =============================================================================
# GROUP & ACCESS MANAGEMENT HANDLERS (Story #742)
# =============================================================================


def _get_group_manager():
    """Get the GroupAccessManager from app.state."""
    return getattr(app_module.app.state, "group_manager", None)


def _validate_group_id(
    args: Dict[str, Any], group_manager: Any
) -> tuple[Optional[int], Any, Optional[Dict[str, Any]]]:
    """Validate and parse group_id, check group exists.

    Returns:
        Tuple of (group_id, group, error_response) - error_response is None on success
    """
    group_id_str = args.get("group_id", "")
    if not group_id_str:
        return (
            None,
            None,
            _mcp_response(
                {"success": False, "error": "Missing required parameter: group_id"}
            ),
        )
    try:
        group_id = int(group_id_str)
    except ValueError:
        return (
            None,
            None,
            _mcp_response(
                {"success": False, "error": f"Invalid group_id: {group_id_str}"}
            ),
        )
    group = group_manager.get_group(group_id)
    if not group:
        return (
            None,
            None,
            _mcp_response({"success": False, "error": f"Group not found: {group_id}"}),
        )
    return group_id, group, None


def handle_list_groups(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all groups with member counts and repository access information."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        groups = group_manager.get_all_groups()
        result_groups = []
        for group in groups:
            member_count = group_manager.get_user_count_in_group(group.id)
            repos = group_manager.get_group_repos(group.id)
            result_groups.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "description": group.description,
                    "member_count": member_count,
                    "repo_count": len(repos),
                }
            )
        return _mcp_response({"success": True, "groups": result_groups})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-129",
                f"Error in handle_list_groups: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_create_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new custom group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        name = args.get("name", "")
        if not name:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: name"}
            )

        try:
            group = group_manager.create_group(
                name=name, description=args.get("description", "")
            )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="group_create",
                target_type="group",
                target_id=str(group.id),
                details=f"Created group '{group.name}' via MCP",
            )
            return _mcp_response(
                {"success": True, "group_id": group.id, "name": group.name}
            )
        except ValueError as e:
            return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-130",
                f"Error in handle_create_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_get_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get detailed information about a specific group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        members = group_manager.get_users_in_group(group_id)
        repos = group_manager.get_group_repos(group_id)
        return _mcp_response(
            {
                "success": True,
                "id": group.id,
                "name": group.name,
                "description": group.description,
                "members": members,
                "repos": repos,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-131",
                f"Error in handle_get_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_update_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Update a custom group's name and/or description."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, _, error = _validate_group_id(args, group_manager)
        if error:
            return error

        try:
            updated_group = group_manager.update_group(
                group_id=group_id,
                name=args.get("name"),
                description=args.get("description"),
            )
            if not updated_group:
                return _mcp_response(
                    {"success": False, "error": f"Group not found: {group_id}"}
                )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="group_update",
                target_type="group",
                target_id=str(group_id),
                details=f"Updated group '{updated_group.name}' via MCP",
            )
            return _mcp_response({"success": True})
        except ValueError as e:
            return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-132",
                f"Error in handle_update_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_delete_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete a custom group."""
    from ..services.group_access_manager import (
        DefaultGroupCannotBeDeletedError,
        GroupHasUsersError,
    )

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error
        group_name = group.name

        try:
            result = group_manager.delete_group(group_id)
            if not result:
                return _mcp_response(
                    {"success": False, "error": f"Group not found: {group_id}"}
                )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="group_delete",
                target_type="group",
                target_id=str(group_id),
                details=f"Deleted group '{group_name}' via MCP",
            )
            return _mcp_response({"success": True})
        except (DefaultGroupCannotBeDeletedError, GroupHasUsersError) as e:
            return _mcp_response({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-133",
                f"Error in handle_delete_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_add_member_to_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Assign a user to a group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        user_id = args.get("user_id", "")
        if not user_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: user_id"}
            )

        group_manager.assign_user_to_group(
            user_id=user_id, group_id=group_id, assigned_by=user.username
        )
        group_manager.log_audit(
            admin_id=user.username,
            action_type="user_group_change",
            target_type="user",
            target_id=user_id,
            details=f"Assigned user '{user_id}' to group '{group.name}' via MCP",
        )
        return _mcp_response({"success": True})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-134",
                f"Error in handle_add_member_to_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_remove_member_from_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Remove a user from a group."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        user_id = args.get("user_id", "")
        if not user_id:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: user_id"}
            )

        group_manager.remove_user_from_group(user_id=user_id, group_id=group_id)
        group_manager.log_audit(
            admin_id=user.username,
            action_type="user_group_change",
            target_type="user",
            target_id=user_id,
            details=f"Removed user '{user_id}' from group '{group.name}' via MCP",
        )
        return _mcp_response({"success": True})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-GENERAL-135",
                f"Error in handle_remove_member_from_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_add_repos_to_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Grant a group access to one or more repositories."""
    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        repo_names = _parse_json_string_array(args.get("repo_names", []))
        if not repo_names:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_names"}
            )

        added_count = 0
        for repo_name in repo_names:
            if group_manager.grant_repo_access(
                repo_name=repo_name, group_id=group_id, granted_by=user.username
            ):
                added_count += 1
                group_manager.log_audit(
                    admin_id=user.username,
                    action_type="repo_access_grant",
                    target_type="repo",
                    target_id=repo_name,
                    details=f"Granted access to '{repo_name}' for group '{group.name}' via MCP",
                )
        return _mcp_response({"success": True, "added_count": added_count})
    except Exception as e:
        logger.error(
            format_error_log(
                "MCP-TOOL-042",
                f"Error in handle_add_repos_to_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_remove_repo_from_group(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Revoke a group's access to a single repository."""
    from ..services.group_access_manager import CidxMetaCannotBeRevokedError

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        repo_name = args.get("repo_name", "")
        if not repo_name:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_name"}
            )

        try:
            if not group_manager.revoke_repo_access(
                repo_name=repo_name, group_id=group_id
            ):
                return _mcp_response(
                    {
                        "success": False,
                        "error": f"Repository '{repo_name}' not found in group's access list",
                    }
                )
            group_manager.log_audit(
                admin_id=user.username,
                action_type="repo_access_revoke",
                target_type="repo",
                target_id=repo_name,
                details=f"Revoked access to '{repo_name}' from group '{group.name}' via MCP",
            )
            return _mcp_response({"success": True})
        except CidxMetaCannotBeRevokedError:
            return _mcp_response(
                {
                    "success": False,
                    "error": "cidx-meta access cannot be revoked from any group",
                }
            )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-001",
                f"Error in handle_remove_repo_from_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_bulk_remove_repos_from_group(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Revoke a group's access to multiple repositories."""
    from ..services.group_access_manager import CidxMetaCannotBeRevokedError
    from ..services.constants import CIDX_META_REPO

    try:
        group_manager = _get_group_manager()
        if not group_manager:
            return _mcp_response(
                {"success": False, "error": "Group manager not configured"}
            )

        group_id, group, error = _validate_group_id(args, group_manager)
        if error:
            return error

        repo_names = _parse_json_string_array(args.get("repo_names", []))
        if not repo_names:
            return _mcp_response(
                {"success": False, "error": "Missing required parameter: repo_names"}
            )

        removed_count = 0
        for repo_name in repo_names:
            if repo_name == CIDX_META_REPO:
                continue
            try:
                if group_manager.revoke_repo_access(
                    repo_name=repo_name, group_id=group_id
                ):
                    removed_count += 1
                    group_manager.log_audit(
                        admin_id=user.username,
                        action_type="repo_access_revoke",
                        target_type="repo",
                        target_id=repo_name,
                        details=f"Revoked access to '{repo_name}' from group '{group.name}' via MCP",
                    )
            except CidxMetaCannotBeRevokedError:
                continue
        return _mcp_response({"success": True, "removed_count": removed_count})
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-002",
                f"Error in handle_bulk_remove_repos_from_group: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["list_groups"] = handle_list_groups
HANDLER_REGISTRY["create_group"] = handle_create_group
HANDLER_REGISTRY["get_group"] = handle_get_group
HANDLER_REGISTRY["update_group"] = handle_update_group
HANDLER_REGISTRY["delete_group"] = handle_delete_group
HANDLER_REGISTRY["add_member_to_group"] = handle_add_member_to_group
HANDLER_REGISTRY["remove_member_from_group"] = handle_remove_member_from_group
HANDLER_REGISTRY["add_repos_to_group"] = handle_add_repos_to_group
HANDLER_REGISTRY["remove_repo_from_group"] = handle_remove_repo_from_group
HANDLER_REGISTRY["bulk_remove_repos_from_group"] = handle_bulk_remove_repos_from_group


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# User Self-Service API Keys
# =============================================================================


def handle_list_api_keys(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all API keys for the authenticated user."""
    try:
        keys = app_module.user_manager.get_api_keys(user.username)
        return _mcp_response(
            {
                "success": True,
                "keys": [
                    {
                        "id": k.get("key_id", k.get("id", "")),
                        "description": k.get("name", k.get("description", "")),
                        "created_at": k.get("created_at", ""),
                        "last_used": k.get("last_used_at"),
                    }
                    for k in keys
                ],
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-003",
                f"Error in handle_list_api_keys: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_create_api_key(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new API key for the authenticated user."""
    try:
        from code_indexer.server.auth.api_key_manager import ApiKeyManager

        description = args.get("description", "")
        api_key_manager = ApiKeyManager(user_manager=app_module.user_manager)
        api_key, key_id = api_key_manager.generate_key(user.username, name=description)
        return _mcp_response(
            {
                "success": True,
                "key_id": key_id,
                "api_key": api_key,
                "description": description,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-004",
                f"Error in handle_create_api_key: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_delete_api_key(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete an API key belonging to the authenticated user."""
    try:
        key_id = args.get("key_id", "")
        if not key_id:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: key_id",
                }
            )

        result = app_module.user_manager.delete_api_key(user.username, key_id)
        return _mcp_response({"success": result})
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-005",
                f"Error in handle_delete_api_key: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["list_api_keys"] = handle_list_api_keys
HANDLER_REGISTRY["create_api_key"] = handle_create_api_key
HANDLER_REGISTRY["delete_api_key"] = handle_delete_api_key


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# User Self-Service MCP Credentials
# =============================================================================


def handle_list_mcp_credentials(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """List all MCP credentials for the authenticated user."""
    try:
        credentials = dependencies.mcp_credential_manager.get_credentials(user.username)
        return _mcp_response(
            {
                "success": True,
                "credentials": [
                    {
                        "id": c.get("credential_id", c.get("id", "")),
                        "description": c.get("name", c.get("description", "")),
                        "created_at": c.get("created_at", ""),
                    }
                    for c in credentials
                ],
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-006",
                f"Error in handle_list_mcp_credentials: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_create_mcp_credential(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new MCP credential for the authenticated user."""
    try:
        description = args.get("description", "")
        result = dependencies.mcp_credential_manager.generate_credential(
            user.username, name=description
        )
        return _mcp_response(
            {
                "success": True,
                "credential_id": result.get("credential_id", ""),
                "credential": result.get("client_secret", ""),
                "client_id": result.get("client_id", ""),
                "client_secret": result.get("client_secret", ""),
                "description": description,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "QUERY-GENERAL-007",
                f"Error in handle_create_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_delete_mcp_credential(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete an MCP credential belonging to the authenticated user."""
    try:
        credential_id = args.get("credential_id", "")
        if not credential_id:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: credential_id",
                }
            )

        result = dependencies.mcp_credential_manager.revoke_credential(
            user.username, credential_id
        )
        return _mcp_response({"success": result})
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-001",
                f"Error in handle_delete_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["list_mcp_credentials"] = handle_list_mcp_credentials
HANDLER_REGISTRY["create_mcp_credential"] = handle_create_mcp_credential
HANDLER_REGISTRY["delete_mcp_credential"] = handle_delete_mcp_credential


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# Admin Operations - Part 1
# =============================================================================


def handle_admin_list_user_mcp_credentials(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """List all MCP credentials for a specific user (admin only)."""
    try:
        username = args.get("username", "")
        if not username:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: username",
                }
            )

        credentials = dependencies.mcp_credential_manager.get_credentials(username)
        return _mcp_response(
            {
                "success": True,
                "credentials": [
                    {
                        "id": c.get("credential_id", c.get("id", "")),
                        "description": c.get("name", c.get("description", "")),
                        "created_at": c.get("created_at", ""),
                    }
                    for c in credentials
                ],
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-002",
                f"Error in handle_admin_list_user_mcp_credentials: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_admin_create_user_mcp_credential(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Create a new MCP credential for a specific user (admin only)."""
    try:
        username = args.get("username", "")
        if not username:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: username",
                }
            )

        description = args.get("description", "")
        result = dependencies.mcp_credential_manager.generate_credential(
            username, name=description
        )
        return _mcp_response(
            {
                "success": True,
                "credential_id": result.get("credential_id", ""),
                "credential": result.get("client_secret", ""),
                "client_id": result.get("client_id", ""),
                "client_secret": result.get("client_secret", ""),
                "description": description,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-003",
                f"Error in handle_admin_create_user_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["admin_list_user_mcp_credentials"] = (
    handle_admin_list_user_mcp_credentials
)
HANDLER_REGISTRY["admin_create_user_mcp_credential"] = (
    handle_admin_create_user_mcp_credential
)


# =============================================================================
# CREDENTIAL MANAGEMENT HANDLERS (Story #743)
# Admin Operations - Part 2
# =============================================================================


def handle_admin_delete_user_mcp_credential(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """Delete an MCP credential for a specific user (admin only)."""
    try:
        username = args.get("username", "")
        credential_id = args.get("credential_id", "")

        if not username:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: username",
                }
            )
        if not credential_id:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Missing required parameter: credential_id",
                }
            )

        result = dependencies.mcp_credential_manager.revoke_credential(
            username, credential_id
        )
        return _mcp_response({"success": result})
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-004",
                f"Error in handle_admin_delete_user_mcp_credential: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_admin_list_all_mcp_credentials(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """List all MCP credentials across all users (admin only)."""
    try:
        all_credentials = []
        all_users = app_module.user_manager.get_all_users()

        for target_user in all_users:
            user_creds = dependencies.mcp_credential_manager.get_credentials(
                target_user.username
            )
            for c in user_creds:
                all_credentials.append(
                    {
                        "id": c.get("credential_id", c.get("id", "")),
                        "username": target_user.username,
                        "description": c.get("name", c.get("description", "")),
                        "created_at": c.get("created_at", ""),
                    }
                )

        return _mcp_response(
            {
                "success": True,
                "credentials": all_credentials,
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-005",
                f"Error in handle_admin_list_all_mcp_credentials: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["admin_delete_user_mcp_credential"] = (
    handle_admin_delete_user_mcp_credential
)
HANDLER_REGISTRY["admin_list_all_mcp_credentials"] = (
    handle_admin_list_all_mcp_credentials
)


# =============================================================================
# ADMIN OPERATIONS MCP HANDLERS (Story #744)
# Audit Logs, Maintenance Mode, and SCIP Administration
# =============================================================================

# Named constants for admin operations
DEFAULT_AUDIT_LOG_LIMIT = 100
JOB_ID_LENGTH = 8


def _filter_audit_entries(
    entries: List[Dict[str, Any]],
    filter_user: Optional[str],
    action: Optional[str],
    from_date: Optional[str],
    to_date: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Filter audit log entries by user, action, and date range."""
    filtered = entries
    if filter_user:
        filtered = [
            e for e in filtered if e.get("user", "").lower() == filter_user.lower()
        ]
    if action:
        filtered = [
            e for e in filtered if action.lower() in e.get("action", "").lower()
        ]
    if from_date:
        filtered = [e for e in filtered if e.get("timestamp", "") >= from_date]
    if to_date:
        filtered = [e for e in filtered if e.get("timestamp", "") <= to_date]
    return filtered[:limit]


def handle_query_audit_logs(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Query security audit logs with optional filtering (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to query audit logs.",
                }
            )

        from code_indexer.server.auth.audit_logger import password_audit_logger

        limit = _coerce_int(args.get("limit"), DEFAULT_AUDIT_LOG_LIMIT)
        pr_logs = password_audit_logger.get_pr_logs(limit=limit)
        cleanup_logs = password_audit_logger.get_cleanup_logs(limit=limit)

        all_entries = [
            {
                "timestamp": log.get("timestamp", ""),
                "user": log.get("repo_alias", ""),
                "action": log.get("event_type", ""),
                "resource": log.get("pr_url", ""),
                "details": log,
            }
            for log in pr_logs
        ] + [
            {
                "timestamp": log.get("timestamp", ""),
                "user": "system",
                "action": log.get("event_type", ""),
                "resource": log.get("repo_path", ""),
                "details": log,
            }
            for log in cleanup_logs
        ]

        filtered = _filter_audit_entries(
            all_entries,
            args.get("user"),
            args.get("action"),
            args.get("from_date"),
            args.get("to_date"),
            limit,
        )
        return _mcp_response(
            {"success": True, "entries": filtered, "total": len(filtered)}
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-006",
                f"Error in handle_query_audit_logs: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_enter_maintenance_mode(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Enter server maintenance mode (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to enter maintenance mode.",
                }
            )

        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        state = get_maintenance_state()
        result = state.enter_maintenance_mode()
        if args.get("message"):
            result["custom_message"] = args["message"]
        return _mcp_response({"success": True, **result})
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-007",
                f"Error in handle_enter_maintenance_mode: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_exit_maintenance_mode(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Exit server maintenance mode (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to exit maintenance mode.",
                }
            )

        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        state = get_maintenance_state()
        result = state.exit_maintenance_mode()
        return _mcp_response({"success": True, **result})
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-008",
                f"Error in handle_exit_maintenance_mode: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["query_audit_logs"] = handle_query_audit_logs
HANDLER_REGISTRY["enter_maintenance_mode"] = handle_enter_maintenance_mode
HANDLER_REGISTRY["exit_maintenance_mode"] = handle_exit_maintenance_mode


def handle_get_maintenance_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get current server maintenance mode status (any authenticated user)."""
    try:
        from code_indexer.server.services.maintenance_service import (
            get_maintenance_state,
        )

        state = get_maintenance_state()
        status = state.get_status()
        return _mcp_response(
            {
                "success": True,
                "in_maintenance": status.get("maintenance_mode", False),
                "message": status.get("message"),
                "since": status.get("entered_at"),
                "drained": status.get("drained", False),
                "running_jobs": status.get("running_jobs", 0),
                "queued_jobs": status.get("queued_jobs", 0),
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-009",
                f"Error in handle_get_maintenance_status: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_scip_pr_history(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get SCIP self-healing PR creation history (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to view SCIP PR history.",
                }
            )

        from code_indexer.server.auth.audit_logger import password_audit_logger

        limit = _coerce_int(args.get("limit"), DEFAULT_AUDIT_LOG_LIMIT)
        pr_logs = password_audit_logger.get_pr_logs(limit=limit)

        history = [
            {
                "pr_number": (
                    log.get("pr_url", "").split("/")[-1] if log.get("pr_url") else None
                ),
                "repo": log.get("repo_alias", ""),
                "indexed_at": log.get("timestamp", ""),
                "status": (
                    "success"
                    if log.get("event_type") == "pr_creation_success"
                    else "failed"
                ),
                "pr_url": log.get("pr_url"),
                "branch_name": log.get("branch_name"),
                "job_id": log.get("job_id"),
            }
            for log in pr_logs
        ]

        return _mcp_response(
            {"success": True, "history": history, "total": len(history)}
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-010",
                f"Error in handle_scip_pr_history: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_scip_cleanup_history(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get SCIP workspace cleanup history (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to view SCIP cleanup history.",
                }
            )

        from code_indexer.server.auth.audit_logger import password_audit_logger

        limit = _coerce_int(args.get("limit"), DEFAULT_AUDIT_LOG_LIMIT)
        cleanup_logs = password_audit_logger.get_cleanup_logs(limit=limit)

        history = [
            {
                "cleanup_id": log.get("timestamp", "")
                .replace(":", "-")
                .replace(".", "-"),
                "started_at": log.get("timestamp", ""),
                "completed_at": log.get("timestamp", ""),
                "workspaces_cleaned": len(log.get("files_cleared", [])),
                "repo_path": log.get("repo_path"),
            }
            for log in cleanup_logs
        ]

        return _mcp_response(
            {"success": True, "history": history, "total": len(history)}
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-011",
                f"Error in handle_scip_cleanup_history: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["get_maintenance_status"] = handle_get_maintenance_status
HANDLER_REGISTRY["scip_pr_history"] = handle_scip_pr_history
HANDLER_REGISTRY["scip_cleanup_history"] = handle_scip_cleanup_history


# Cleanup job state tracking for scip_cleanup_workspaces/scip_cleanup_status
_cleanup_job_state: Dict[str, Any] = {
    "running": False,
    "job_id": None,
    "progress": None,
    "last_result": None,
}


def _execute_workspace_cleanup() -> Dict[str, Any]:
    """Execute workspace cleanup and return result dict."""
    workspace_cleanup_service = getattr(
        app_module.app.state, "workspace_cleanup_service", None
    )
    if workspace_cleanup_service:
        result = workspace_cleanup_service.cleanup_workspaces()
        return {
            "workspaces_scanned": result.workspaces_scanned,
            "workspaces_deleted": result.workspaces_deleted,
            "workspaces_preserved": result.workspaces_preserved,
            "space_reclaimed_bytes": result.space_reclaimed_bytes,
            "duration_seconds": result.duration_seconds,
            "errors": result.errors,
        }
    return {"message": "Workspace cleanup service not available"}


def handle_scip_cleanup_workspaces(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Trigger SCIP workspace cleanup job (admin only)."""
    global _cleanup_job_state
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to trigger SCIP cleanup.",
                }
            )

        if _cleanup_job_state["running"]:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Cleanup job already running",
                    "job_id": _cleanup_job_state["job_id"],
                }
            )

        import uuid

        job_id = str(uuid.uuid4())[:JOB_ID_LENGTH]

        _cleanup_job_state.update(
            {"running": True, "job_id": job_id, "progress": "started"}
        )
        try:
            _cleanup_job_state["last_result"] = _execute_workspace_cleanup()
            _cleanup_job_state["progress"] = "completed"
        except Exception as cleanup_error:
            logger.error(
                format_error_log(
                    "REPO-GENERAL-012",
                    f"Workspace cleanup failed: {cleanup_error}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            _cleanup_job_state["progress"] = f"failed: {str(cleanup_error)}"
        finally:
            _cleanup_job_state["running"] = False

        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "status": _cleanup_job_state["progress"],
            }
        )
    except Exception as e:
        _cleanup_job_state["running"] = False
        logger.error(
            format_error_log(
                "REPO-GENERAL-013",
                f"Error in handle_scip_cleanup_workspaces: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


def handle_scip_cleanup_status(args: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Get SCIP workspace cleanup job status (admin only)."""
    try:
        if user.role != UserRole.ADMIN:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Permission denied. Admin role required to view cleanup status.",
                }
            )

        workspace_cleanup_service = getattr(
            app_module.app.state, "workspace_cleanup_service", None
        )
        service_status = (
            workspace_cleanup_service.get_cleanup_status()
            if workspace_cleanup_service
            else {}
        )

        return _mcp_response(
            {
                "success": True,
                "running": _cleanup_job_state["running"],
                "job_id": _cleanup_job_state["job_id"],
                "progress": _cleanup_job_state["progress"],
                "last_cleanup_time": service_status.get("last_cleanup_time"),
                "workspace_count": service_status.get("workspace_count", 0),
                "oldest_workspace_age": service_status.get("oldest_workspace_age"),
                "total_size_mb": service_status.get("total_size_mb", 0.0),
                "last_result": _cleanup_job_state.get("last_result"),
            }
        )
    except Exception as e:
        logger.error(
            format_error_log(
                "REPO-GENERAL-014",
                f"Error in handle_scip_cleanup_status: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


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


def handle_trigger_dependency_analysis(
    args: Dict[str, Any], user: User
) -> Dict[str, Any]:
    """
    Trigger dependency map analysis manually (Story #195).

    Args:
        args: Tool arguments with optional mode ("full" or "delta")
        user: The authenticated user making the request

    Returns:
        MCP response with job_id, mode, and status
    """
    import threading
    import uuid
    from datetime import datetime, timezone
    from code_indexer.server.utils.config_manager import ServerConfigManager

    try:
        # AC4: Default mode is delta
        mode = args.get("mode", "delta") or "delta"

        # AC8: Validate mode parameter
        if mode not in ["full", "delta"]:
            return _mcp_response(
                {
                    "success": False,
                    "error": f"Invalid mode '{mode}'. Must be 'full' or 'delta'.",
                    "job_id": None,
                }
            )

        # AC6: Check if feature is enabled
        _scm = ServerConfigManager()
        _server_config = _scm.load_config()
        _ci_config = _server_config.claude_integration_config if _server_config else None
        if not _ci_config or not getattr(_ci_config, "dependency_map_enabled", False):
            return _mcp_response(
                {
                    "success": False,
                    "error": "Dependency map analysis is disabled",
                    "job_id": None,
                }
            )

        # AC5: Check if analysis is already running
        dependency_map_service = getattr(app_module.app.state, "dependency_map_service", None)
        if not dependency_map_service:
            return _mcp_response(
                {
                    "success": False,
                    "error": "Dependency map service not available",
                    "job_id": None,
                }
            )

        if not dependency_map_service.is_available():
            return _mcp_response(
                {
                    "success": False,
                    "error": "Dependency map analysis already in progress",
                    "job_id": None,
                }
            )

        # Generate job ID
        job_id = f"dep-map-{mode}-{uuid.uuid4().hex[:8]}-{int(datetime.now(timezone.utc).timestamp())}"

        # AC2/AC3: Spawn background thread for analysis
        def run_analysis_job():
            """Background job to run dependency map analysis."""
            try:
                if mode == "full":
                    dependency_map_service.run_full_analysis()
                else:
                    dependency_map_service.run_delta_analysis()
            except Exception as e:
                logger.error(
                    format_error_log(
                        "DEPMAP-TRIGGER-001",
                        f"Background dependency map analysis failed: {e}",
                        extra={"correlation_id": get_correlation_id(), "job_id": job_id},
                    )
                )

        # Start background daemon thread
        thread = threading.Thread(target=run_analysis_job, daemon=True)
        thread.start()

        # AC2/AC3: Return job_id immediately
        return _mcp_response(
            {
                "success": True,
                "job_id": job_id,
                "mode": mode,
                "status": "queued",
                "message": f"Dependency map {mode} analysis started",
            }
        )

    except Exception as e:
        logger.error(
            format_error_log(
                "DEPMAP-TRIGGER-002",
                f"Error triggering dependency map analysis: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response(
            {"success": False, "error": str(e), "job_id": None}
        )


HANDLER_REGISTRY["scip_cleanup_workspaces"] = handle_scip_cleanup_workspaces
HANDLER_REGISTRY["scip_cleanup_status"] = handle_scip_cleanup_status
HANDLER_REGISTRY["start_trace"] = handle_start_trace
HANDLER_REGISTRY["end_trace"] = handle_end_trace
HANDLER_REGISTRY["list_repo_categories"] = list_repo_categories
HANDLER_REGISTRY["trigger_dependency_analysis"] = handle_trigger_dependency_analysis


# ---------------------------------------------------------------------------
# Wiki Article Analytics (Story #293)
# ---------------------------------------------------------------------------

# Minimum search query length to trigger CIDX search filter
_WIKI_ANALYTICS_MIN_QUERY_LENGTH = 2
# Max CIDX results to use as path filter (100 is sufficient for wiki article lists)
_WIKI_ANALYTICS_MAX_SEARCH_RESULTS = 100


def _get_wiki_cache_for_handler():
    """Return a WikiCache instance using golden_repo_manager.db_path.

    Instantiates WikiCache on each call (lightweight - no I/O until a method
    is called). Returns None when golden_repo_manager is unavailable.
    """
    from code_indexer.server.wiki.wiki_cache import WikiCache

    grm = getattr(app_module, "golden_repo_manager", None)
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
    sqm = getattr(app_module, "semantic_query_manager", None)
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
        articles.append({
            "title": title,
            "path": path,
            "real_views": v["real_views"],
            "first_viewed_at": v["first_viewed_at"],
            "last_viewed_at": v["last_viewed_at"],
            "wiki_url": f"/wiki/{wiki_alias}/{path_no_ext}",
        })
    return articles


def handle_wiki_article_analytics(
    params: Dict[str, Any], user: User
) -> Dict[str, Any]:
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
            return _mcp_response({
                "success": False,
                "error": "Wiki is not enabled for this repository",
            })

        # AC4: Optional search filter - raises on sqm unavailability
        article_paths_filter = _wiki_analytics_filter_by_search(
            repo_alias, search_query or "", search_mode, user.username
        )
        if article_paths_filter is not None and not article_paths_filter:
            return _mcp_response({
                "success": True,
                "articles": [],
                "total_count": 0,
                "repo_alias": repo_alias,
                "sort_by": sort_by,
                "wiki_enabled": True,
            })

        wiki_cache = _get_wiki_cache_for_handler()
        if wiki_cache is None:
            return _mcp_response({
                "success": False,
                "error": "Wiki cache not available",
            })

        all_views = wiki_cache.get_all_view_counts(wiki_alias)

        if article_paths_filter is not None:
            all_views = [v for v in all_views if v["article_path"] in article_paths_filter]

        # AC3: Sort with alphabetical tie-breaking
        if sort_by == "least_viewed":
            all_views.sort(key=lambda x: (x["real_views"], x["article_path"]))
        else:
            all_views.sort(key=lambda x: (-x["real_views"], x["article_path"]))

        all_views = all_views[:limit]
        articles = _wiki_analytics_build_articles(all_views, wiki_alias)

        return _mcp_response({
            "success": True,
            "articles": articles,
            "total_count": len(articles),
            "repo_alias": repo_alias,
            "sort_by": sort_by,
            "wiki_enabled": True,
        })

    except Exception as e:
        logger.warning(
            format_error_log(
                "WIKI-ANALYTICS-001",
                f"Error in handle_wiki_article_analytics: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return _mcp_response({"success": False, "error": str(e)})


HANDLER_REGISTRY["wiki_article_analytics"] = handle_wiki_article_analytics
