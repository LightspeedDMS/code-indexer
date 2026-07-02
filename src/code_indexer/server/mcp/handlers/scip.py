"""SCIP code intelligence handlers for CIDX MCP server.

Covers: symbol definition, references, dependencies, dependents,
impact analysis, call chains, smart context, SCIP audit log,
PR history, cleanup history, cleanup workspaces, and cleanup status.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Any, Optional, List

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.logging_utils import format_error_log
from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.mcp import reranking as _mcp_reranking

from . import _utils
from ._utils import (
    _mcp_response,
    _coerce_int,
    _coerce_float,
    _get_scip_query_service,
    _get_scip_audit_repository,
    _apply_scip_payload_truncation,
    _validate_symbol_format,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Named constants for admin operations (mirrors _legacy.py values)
DEFAULT_AUDIT_LOG_LIMIT = 100
JOB_ID_LENGTH = 8

# Story #659: Default overfetch multiplier when rerank_config is unavailable.
_DEFAULT_OVERFETCH_MULTIPLIER = 5
# Story #658: Maximum number of results to fetch when overfetching for reranking.
_MAX_RERANK_FETCH_LIMIT = 200

# Timeout for scip_context queries
_SCIP_CONTEXT_TIMEOUT_SECONDS = 30

# Audit log pagination bounds
_MIN_AUDIT_LIMIT = 1
_MAX_AUDIT_LIMIT = 1000

# Maximum chains returned from scip_callchain
_MAX_CALL_CHAINS_RETURNED = 100

# Cleanup job state tracking for scip_cleanup_workspaces/scip_cleanup_status
_cleanup_job_state: Dict[str, Any] = {
    "running": False,
    "job_id": None,
    "progress": None,
    "last_result": None,
}


# ---------------------------------------------------------------------------
# Private helper: overfetch limit computation
# ---------------------------------------------------------------------------


def _compute_fetch_limit(requested_limit: int, rerank_query: Optional[str]) -> int:
    """Compute the actual fetch limit, overfetching when reranking is requested."""
    if not rerank_query:
        return requested_limit
    try:
        config = get_config_service().get_config()
        rerank_cfg = getattr(config, "rerank_config", None) if config else None
        multiplier = (
            getattr(rerank_cfg, "overfetch_multiplier", _DEFAULT_OVERFETCH_MULTIPLIER)
            if rerank_cfg
            else _DEFAULT_OVERFETCH_MULTIPLIER
        )
    except Exception as e:
        logger.warning(
            "Failed to get reranker config, using default overfetch multiplier: %s", e
        )
        multiplier = _DEFAULT_OVERFETCH_MULTIPLIER
    return min(requested_limit * multiplier, _MAX_RERANK_FETCH_LIMIT)


# ---------------------------------------------------------------------------
# Private helpers: audit log parsing
# ---------------------------------------------------------------------------


def _filter_audit_entries(
    entries: List[Dict[str, Any]],
    filter_user: Optional[str],
    action: Optional[str],
    from_date: Optional[str],
    to_date: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Filter audit log entries by user, action, and date range.

    Used by both handle_scip_pr_history/handle_scip_cleanup_history (this module)
    and handle_query_audit_logs (currently in _legacy.py, to be extracted to
    admin.py in a future Story #496 step).
    """
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


def _parse_log_details(row: dict) -> dict:
    """Parse the details JSON field of an audit_logs row into a flat dict.

    When AuditLogService rows are used instead of flat-file parsed dicts the
    payload lives inside the ``details`` JSON column.  This helper merges the
    top-level row fields with the decoded details so callers get the same shape
    that the old PasswordChangeAuditLogger flat-file parsing produced.
    """
    flat = dict(row)
    details_str = row.get("details") or "{}"
    try:
        inner = json.loads(details_str)
    except (ValueError, TypeError) as e:
        logger.warning("Failed to parse audit log details JSON: %s", e)
        inner = {}
    flat.update(inner)
    return flat


def _get_pr_logs_from_service(limit: int, repo_alias: Optional[str] = None) -> list:
    """Fetch PR logs from AuditLogService."""

    svc = getattr(getattr(_utils.app_module, "app", None), "state", None)
    audit_service = getattr(svc, "audit_service", None) if svc else None
    if audit_service is None:
        raise RuntimeError("AuditLogService not available on app.state")
    rows = audit_service.get_pr_logs(repo_alias=repo_alias, limit=limit)
    return [_parse_log_details(r) for r in rows]


def _get_cleanup_logs_from_service(limit: int, repo_path: Optional[str] = None) -> list:
    """Fetch cleanup logs from AuditLogService."""

    svc = getattr(getattr(_utils.app_module, "app", None), "state", None)
    audit_service = getattr(svc, "audit_service", None) if svc else None
    if audit_service is None:
        raise RuntimeError("AuditLogService not available on app.state")
    rows = audit_service.get_cleanup_logs(repo_path=repo_path, limit=limit)
    return [_parse_log_details(r) for r in rows]


# ---------------------------------------------------------------------------
# Private helper: workspace cleanup executor
# ---------------------------------------------------------------------------


def _execute_workspace_cleanup() -> Dict[str, Any]:
    """Execute workspace cleanup and return result dict."""
    workspace_cleanup_service = getattr(
        _utils.app_module.app.state, "workspace_cleanup_service", None
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


# ---------------------------------------------------------------------------
# Private helpers: scip_callchain support
# ---------------------------------------------------------------------------


def _deduplicate_and_sort_chains(all_chains: list) -> list:
    """Deduplicate call chains by path and sort by length (shortest first)."""
    unique_chains_map = {}
    for chain in all_chains:
        path_key = tuple(chain.get("path", []))
        if path_key not in unique_chains_map:
            unique_chains_map[path_key] = chain
    unique_chains = list(unique_chains_map.values())
    unique_chains.sort(key=lambda c: c.get("length", 0))
    return unique_chains


def _generate_callchain_diagnostic(
    from_symbol: Optional[str], to_symbol: Optional[str], chain_count: int
) -> Optional[str]:
    """Generate a diagnostic message when no call chains are found."""
    if chain_count > 0:
        return None
    diagnostic = f"No call chains found from '{from_symbol}' to '{to_symbol}'. "
    diagnostic += (
        "Verify symbol names exist in the codebase. "
        "Try using simple class or method names."
    )
    return diagnostic


# ---------------------------------------------------------------------------
# Private helper: audit log pagination validation
# ---------------------------------------------------------------------------


def _validate_audit_pagination(limit: Any, offset: Any) -> tuple:
    """Parse and clamp audit log pagination parameters.

    Returns:
        (limit, offset) as validated integers.
    """
    try:
        limit_int = int(limit)
        offset_int = int(offset)
        limit_int = max(_MIN_AUDIT_LIMIT, min(limit_int, _MAX_AUDIT_LIMIT))
        offset_int = max(0, offset_int)
    except (ValueError, TypeError):
        limit_int = 100
        offset_int = 0
    return limit_int, offset_int


# ---------------------------------------------------------------------------
# Public SCIP query handlers
# ---------------------------------------------------------------------------


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
            - rerank_query: Optional query for cross-encoder reranking (Story #659)
            - rerank_instruction: Optional instruction prefix for reranker (Story #659)
        user: Authenticated user (for permission checking)

    Returns:
        MCP-compliant response with reference results
    """
    try:
        symbol = params.get("symbol")
        requested_limit = _coerce_int(params.get("limit"), 100)
        exact = params.get("exact", False)
        project = params.get("project")
        repository_alias = params.get("repository_alias")
        # Story #659: Optional reranking parameters
        rerank_query = params.get("rerank_query") or None
        rerank_instruction = params.get("rerank_instruction")

        if not symbol:
            return _mcp_response(
                {"success": False, "error": "symbol parameter is required"}
            )

        # Story #659: Overfetch when reranking is requested so the reranker
        # has a larger candidate pool; truncate back to requested_limit after reranking.
        fetch_limit = _compute_fetch_limit(requested_limit, rerank_query)

        # Delegate to SCIPQueryService (Story #40)
        service = _get_scip_query_service()
        results_dicts = service.find_references(
            symbol=symbol,
            limit=fetch_limit,
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

        # Story #659: Apply cross-encoder reranking after retrieval, before return.
        # Guard: skip entirely when no rerank_query to avoid overhead.
        if rerank_query:
            results_dicts, _rerank_meta = _mcp_reranking._apply_reranking_sync(
                results=results_dicts,
                rerank_query=rerank_query,
                rerank_instruction=rerank_instruction,
                content_extractor=lambda r: r.get("context") or "",
                requested_limit=requested_limit,
                config_service=get_config_service(),
            )
        else:
            _rerank_meta = {
                "reranker_used": False,
                "reranker_provider": None,
                "rerank_time_ms": 0,
            }

        return _mcp_response(
            {
                "success": True,
                "symbol": symbol,
                "total_results": len(results_dicts),
                "results": results_dicts,
                "query_metadata": {
                    "reranker_used": _rerank_meta["reranker_used"],
                    "reranker_provider": _rerank_meta["reranker_provider"],
                    "rerank_time_ms": _rerank_meta["rerank_time_ms"],
                },
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
            limit=_MAX_CALL_CHAINS_RETURNED,
            repository_alias=repository_alias,
            username=user.username,
        )

        unique_chains = _deduplicate_and_sort_chains(all_chains)
        max_depth_reached = any(
            chain.get("length", 0) >= max_depth for chain in unique_chains
        )
        truncated = len(unique_chains) > _MAX_CALL_CHAINS_RETURNED
        returned_chains = unique_chains[:_MAX_CALL_CHAINS_RETURNED]
        diagnostic = _generate_callchain_diagnostic(
            from_symbol, to_symbol, len(unique_chains)
        )

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
    from code_indexer.scip.database.queries import QueryTimeoutError

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
            timeout_seconds=_SCIP_CONTEXT_TIMEOUT_SECONDS,
        )

        return _mcp_response(
            {
                "success": True,
                **result,
            }
        )
    except QueryTimeoutError as e:
        logger.warning(
            f"scip_context timeout for symbol: {params.get('symbol')}: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response(
            {
                "success": False,
                "error": f"Query timeout exceeded: {e}",
                "files": [],
            }
        )
    except Exception as e:
        logger.exception(
            f"Error in scip_context: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
        return _mcp_response({"success": False, "error": str(e), "files": []})


# ---------------------------------------------------------------------------
# SCIP audit log handler
# ---------------------------------------------------------------------------


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

        limit, offset = _validate_audit_pagination(
            params.get("limit", 100), params.get("offset", 0)
        )

        # Query audit repository
        records, total = _get_scip_audit_repository().query_audit_records(
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


# ---------------------------------------------------------------------------
# SCIP self-healing PR/cleanup history handlers
# ---------------------------------------------------------------------------


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

        limit = _coerce_int(args.get("limit"), DEFAULT_AUDIT_LOG_LIMIT)
        pr_logs = _get_pr_logs_from_service(limit=limit)

        history = [
            {
                "pr_number": (
                    log.get("pr_url", "").split("/")[-1] if log.get("pr_url") else None
                ),
                "repo": log.get("repo_alias", ""),
                "indexed_at": log.get("timestamp", ""),
                "status": (
                    "success"
                    if (log.get("event_type") or log.get("action_type"))
                    == "pr_creation_success"
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
    except RuntimeError as e:
        logger.critical("AuditLogService configuration error: %s", e)
        return _mcp_response(
            {"success": False, "error": f"Server configuration error: {e}"}
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

        limit = _coerce_int(args.get("limit"), DEFAULT_AUDIT_LOG_LIMIT)
        cleanup_logs = _get_cleanup_logs_from_service(limit=limit)

        history = [
            {
                "cleanup_id": log.get("timestamp", "")
                .replace(":", "-")
                .replace(".", "-"),
                "started_at": log.get("timestamp", ""),
                "completed_at": log.get("timestamp", ""),
                "workspaces_cleaned": len(log.get("files_cleared", [])),
                "repo_path": log.get("repo_path") or log.get("target_id"),
            }
            for log in cleanup_logs
        ]

        return _mcp_response(
            {"success": True, "history": history, "total": len(history)}
        )
    except RuntimeError as e:
        logger.critical("AuditLogService configuration error: %s", e)
        return _mcp_response(
            {"success": False, "error": f"Server configuration error: {e}"}
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


# ---------------------------------------------------------------------------
# SCIP workspace cleanup handlers
# ---------------------------------------------------------------------------


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
            _utils.app_module.app.state, "workspace_cleanup_service", None
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


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _register(registry: dict) -> None:
    """Register SCIP handlers into HANDLER_REGISTRY."""
    registry["scip_definition"] = scip_definition
    registry["scip_references"] = scip_references
    registry["scip_dependencies"] = scip_dependencies
    registry["scip_dependents"] = scip_dependents
    registry["scip_impact"] = scip_impact
    registry["scip_callchain"] = scip_callchain
    registry["scip_context"] = scip_context
    registry["get_scip_audit_log"] = get_scip_audit_log
    registry["scip_pr_history"] = handle_scip_pr_history
    registry["scip_cleanup_history"] = handle_scip_cleanup_history
    registry["scip_cleanup_workspaces"] = handle_scip_cleanup_workspaces
    registry["scip_cleanup_status"] = handle_scip_cleanup_status
