"""Shared utility functions for MCP handler modules.

This module is the leaf of the handlers package -- it MUST NOT import from any
sibling domain module (search, repos, files, etc.).  All domain modules import
from here; nothing here imports from them.
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import difflib
import json
import logging
import pathspec
from dataclasses import dataclass
from typing import Dict, Any, Optional, List, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.services.hnsw_health_service import HNSWHealthService
from code_indexer.server.auth.user_manager import User
from code_indexer.server import app as app_module
from code_indexer.server.repositories.scip_audit import SCIPAuditRepository
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.services.config_service import get_config_service

logger = logging.getLogger(__name__)

HTTP_STATUS_BAD_REQUEST = 400


@dataclass
class CapBreach:
    """Carries the fields of a cap violation (wildcard expansion or total fan-out).

    Returned by _check_wildcard_cap (per-pattern wildcard breach) and
    _enforce_repo_count_cap (per-search total alias count breach).
    Callers convert to the format appropriate for their transport:
    MCP envelope or HTTP 400.

    error_code distinguishes breach type in the MCP envelope:
      - "wildcard_cap_exceeded"   — per-pattern wildcard expansion cap (Bug #881)
      - "repo_count_cap_exceeded" — total alias fan-out cap (Bug #894)
    """

    pattern: str
    observed_count: int
    configured_cap: int
    error_code: str = "wildcard_cap_exceeded"


def _cap_breach_message(breach: CapBreach) -> str:
    """Build the human-readable message for a wildcard cap breach.

    Shared by cap_breach_response and cap_breach_http_exception.

    Raises:
        ValueError: If breach is None.
    """
    if breach is None:
        raise ValueError("breach must not be None")
    return (
        f"Wildcard pattern {breach.pattern!r} expanded to {breach.observed_count} "
        f"repositories, exceeding the server cap of {breach.configured_cap}. "
        "Narrow the pattern or pass an explicit list of repository aliases."
    )


def _check_wildcard_cap(
    pattern: str, observed_count: int, configured_cap: int
) -> "Optional[CapBreach]":
    """Return CapBreach when observed_count exceeds configured_cap, else None.

    The boundary is inclusive: observed_count == configured_cap is not a breach.

    Raises:
        ValueError: If pattern is None/empty or either count is negative.
    """
    if not pattern:
        raise ValueError("pattern must be a non-empty string")
    if observed_count < 0:
        raise ValueError(f"observed_count must be non-negative, got {observed_count}")
    if configured_cap < 0:
        raise ValueError(f"configured_cap must be non-negative, got {configured_cap}")
    if observed_count > configured_cap:
        return CapBreach(
            pattern=pattern,
            observed_count=observed_count,
            configured_cap=configured_cap,
        )
    return None


def cap_breach_response(breach: CapBreach) -> "Dict[str, Any]":
    """Build an MCP envelope dict for a cap breach (wildcard or total fan-out).

    Uses breach.error_code to distinguish wildcard vs repo-count breaches.
    """
    if breach is None:
        raise ValueError("breach must not be None")
    payload = {
        "success": False,
        "error": breach.error_code,
        "pattern": breach.pattern,
        "observed": breach.observed_count,
        "cap": breach.configured_cap,
        "remediation": _cap_breach_message(breach),
    }
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def cap_breach_http_exception(breach: CapBreach) -> None:
    """Raise HTTPException(400) for a wildcard cap breach."""
    if breach is None:
        raise ValueError("breach must not be None")
    from fastapi import HTTPException

    raise HTTPException(
        status_code=HTTP_STATUS_BAD_REQUEST,
        detail=_cap_breach_message(breach),
    )


# Fallback SCIP Audit Repository for standalone/SQLite mode (no backend)
_scip_audit_repository_fallback: Optional[SCIPAuditRepository] = None


def _get_scip_audit_repository() -> SCIPAuditRepository:
    """Get the SCIPAuditRepository instance.

    Returns the backend-aware instance from app.state when available
    (set during startup in app_wiring.py), otherwise falls back to a
    module-level SQLite singleton for backward compatibility.
    """
    global _scip_audit_repository_fallback
    repo = getattr(app_module.app.state, "scip_audit_repository", None)
    if repo is not None:
        return repo
    if _scip_audit_repository_fallback is None:
        _scip_audit_repository_fallback = SCIPAuditRepository()
    return _scip_audit_repository_fallback


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
        return {repo["alias"] for repo in all_repos if repo.get("wiki_enabled", False)}
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


def _list_global_repos() -> list:
    """List all global repos from the storage backend (SQLite or PostgreSQL).

    Returns list of dicts with keys: alias_name, repo_name, repo_url,
    index_path, created_at, last_refresh, enable_temporal, etc.
    Works identically in standalone and cluster mode via BackendRegistry.
    """
    return list(
        app_module.app.state.backend_registry.global_repos.list_repos().values()
    )


def _get_global_repo(alias_name: str):
    """Get a single global repo by alias from the storage backend.

    Returns dict with repo details, or None if not found.
    Works identically in standalone and cluster mode via BackendRegistry.
    """
    return app_module.app.state.backend_registry.global_repos.get_repo(alias_name)


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


def _get_access_filtering_service():
    """Get the AccessFilteringService if configured, None otherwise (Story #300).

    Returns:
        AccessFilteringService instance if configured in app.state, None otherwise.
    """
    return getattr(app_module.app.state, "access_filtering_service", None)


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
    access_filtering_service = _get_access_filtering_service()

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


def _get_available_repos(user: "User") -> List[str]:
    """Get list of available global repository aliases for suggestions.

    Story #331 AC1: Filters repos through AccessFilteringService so that
    restricted users only see repos they have access to in error suggestions.

    Args:
        user: The authenticated user requesting the repo list.

    Returns:
        List of repository alias names the user can see.
    """
    try:
        all_repos = [r["alias_name"] for r in _list_global_repos()]
        access_service = _get_access_filtering_service()
        if access_service:
            filtered: List[str] = access_service.filter_repo_listing(
                all_repos, user.username
            )
            return filtered
        return all_repos
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
    temporal_params = [
        "time_range",
        "time_range_all",
        "at_commit",
        "include_removed",
        "chunk_type",
        "diff_type",
        "author",
    ]
    return any(params.get(p) for p in temporal_params)


def _get_temporal_status(repo_aliases: List[str]) -> Dict[str, Any]:
    """Get temporal indexing status for each repository.

    Args:
        repo_aliases: List of repository aliases to check

    Returns:
        Dict with temporal_repos, non_temporal_repos, and optional warning
    """
    try:
        all_repos = {r["alias_name"]: r for r in _list_global_repos()}

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


def _enforce_wildcard_cap(
    wildcard_match_counts: Dict[str, int],
) -> "Optional[CapBreach]":
    """Check per-pattern wildcard match counts against omni_wildcard_expansion_cap.

    Reads cap fresh from config on each call so hot-reload takes effect
    without a server restart.  Literal (non-wildcard) patterns are never passed
    to this helper and are therefore never capped.

    Args:
        wildcard_match_counts: Mapping of wildcard pattern -> match count.

    Returns:
        First CapBreach found, or None if all patterns are within cap.
    """
    if not wildcard_match_counts:
        return None
    cap = (
        get_config_service()
        .get_config()
        .multi_search_limits_config.omni_wildcard_expansion_cap
    )
    for pattern, count in wildcard_match_counts.items():
        breach = _check_wildcard_cap(pattern, count, cap)
        if breach is not None:
            logger.warning(
                format_error_log(
                    "MCP-GENERAL-032",
                    f"Wildcard expansion cap exceeded: pattern={pattern!r} "
                    f"matched={count} cap={cap}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return breach
    return None


def _enforce_repo_count_cap(aliases: List[str]) -> "Optional[CapBreach]":
    """Bug #894: cap total repositories in a single omni search fan-out.

    Enforces omni_max_repos_per_search AFTER wildcard expansion + literal union.
    Returns CapBreach if len(aliases) exceeds the configured cap; None otherwise.

    Analog of _enforce_wildcard_cap, but per-search-total rather than per-pattern.
    Reads cap fresh from config on each call so hot-reload takes effect without
    a server restart.

    Args:
        aliases: Final merged alias list (post-expansion, post-dedup).

    Returns:
        CapBreach with error_code="repo_count_cap_exceeded" if count exceeds cap,
        None otherwise.
    """
    if not aliases:
        return None
    cap = (
        get_config_service()
        .get_config()
        .multi_search_limits_config.omni_max_repos_per_search
    )
    count = len(aliases)
    if count > cap:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-033",
                f"Total repo count cap exceeded: count={count} cap={cap}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return CapBreach(
            pattern=f"<{count} aliases>",
            observed_count=count,
            configured_cap=cap,
            error_code="repo_count_cap_exceeded",
        )
    return None


def _expand_wildcard_patterns(
    patterns: List[str], user: "User"
) -> "Union[List[str], CapBreach]":
    """Expand wildcard patterns to matching repository aliases.

    Story #331 AC2: Accepts a user parameter and filters the available_repos
    list through AccessFilteringService before wildcard matching, so that
    restricted users only see repos they have access to.

    Bug #881 Phase 3: Returns CapBreach if a wildcard pattern matched more
    aliases than omni_wildcard_expansion_cap. Literal patterns bypass the cap.

    Args:
        patterns: List of repo patterns (may include wildcards like '*-global')
        user: The authenticated user requesting the expansion.

    Returns:
        Expanded list of unique repository aliases, or CapBreach on cap breach.
    """
    golden_repos_dir = _get_golden_repos_dir()
    if not golden_repos_dir:
        logger.debug(
            "No golden_repos_dir, returning patterns unchanged",
            extra={"correlation_id": get_correlation_id()},
        )
        return patterns

    try:
        available_repos = [r["alias_name"] for r in _list_global_repos()]
    except Exception as e:
        logger.warning(
            format_error_log(
                "MCP-GENERAL-029",
                f"Failed to list global repos for wildcard expansion: {e}",
                extra={"correlation_id": get_correlation_id()},
            )
        )
        return patterns

    # Story #331 AC2: Filter available repos through access control
    access_service = _get_access_filtering_service()
    if access_service and not access_service.is_admin_user(user.username):
        available_repos = access_service.filter_repo_listing(
            available_repos, user.username
        )

    expanded: List[str] = []
    _wildcard_match_counts: Dict[str, int] = {}
    for pattern in patterns:
        if _has_wildcard(pattern):
            spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
            matches = [repo for repo in available_repos if spec.match_file(repo)]
            if matches:
                # Bug #881 Phase 1: promoted from DEBUG to INFO
                logger.info(
                    f"Wildcard expansion: pattern={pattern!r} matched_count={len(matches)} "
                    f"correlation_id={get_correlation_id()!r} "
                    f"matches={matches}",
                    extra={"correlation_id": get_correlation_id()},
                )
                # Bug #881 Phase 3: track per-pattern count for cap enforcement
                _wildcard_match_counts[pattern] = len(matches)
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
            # Literal pattern — never subject to expansion cap
            expanded.append(pattern)

    # Deduplicate while preserving order
    seen: set = set()
    result: List[str] = []
    for repo in expanded:
        if repo not in seen:
            seen.add(repo)
            result.append(repo)

    # Bug #881 Phase 3: enforce cap after deduplication using per-pattern counts
    cap_breach = _enforce_wildcard_cap(_wildcard_match_counts)
    if cap_breach is not None:
        return cap_breach

    return result
