"""
Multi-Repository Query REST API Routes.

Provides /api/query/multi endpoint for executing searches across multiple
repositories in parallel with proper authentication and error handling.

Implements AC1: REST endpoint for multi-repository search.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import Optional, Dict, List, Any

from code_indexer.server.logging_utils import format_error_log, get_log_extra

from ..auth.dependencies import get_current_user
from ..auth.user_manager import User
from ..multi import (
    MultiSearchService,
    MultiSearchConfig,
    MultiSearchRequest,
    MultiSearchResponse,
)
from code_indexer.server.services.api_metrics_service import api_metrics_service
from code_indexer.server.services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
    RepoAccessDeniedError,
    require_repo_access,
)

logger = logging.getLogger(__name__)


def _enforce_repo_access(
    access_filtering_service: Optional[Any],
    username: str,
    aliases: List[str],
) -> None:
    """Enforce repo-level access for every repository in a multi-repo request.

    /api/query/multi authorizes every alias listed in `repositories` against
    the caller's group grants via AccessFilteringService before any
    semantic, FTS, regex, or temporal (git-history) search (all four
    search modalities, inherently multi-repo by design). Delegates the
    actual access decision to the shared require_repo_access() guard
    (same semantics as the MCP dispatcher's _check_repository_access())
    and shapes the result into this route's HTTPException conventions.

    Called UNCONDITIONALLY, before MultiSearchService.search() executes,
    so the check can never be silently skipped and no repo is searched
    before every requested alias is proven accessible (no silent partial
    results).

    Raises:
        HTTPException 403: caller lacks access to one of the requested
            aliases (checked in order).
        HTTPException 500: access_filtering_service is unavailable --
            fails closed rather than skipping the check.
    """
    try:
        require_repo_access(access_filtering_service, username, aliases)
    except RepoAccessDeniedError as e:
        raise HTTPException(
            status_code=403,
            detail={"error_code": "access_denied", "detail": str(e)},
        )
    except AccessFilteringServiceUnavailableError as e:
        raise HTTPException(
            status_code=500,
            detail={"error_code": "access_control_unavailable", "detail": str(e)},
        )


def _apply_multi_truncation(
    grouped_results: Dict[str, List[Dict[str, Any]]], search_type: str
) -> Dict[str, List[Dict[str, Any]]]:
    """Apply payload truncation to multi-repo grouped search results (Story #683).

    Story #50: Converted from async to sync since underlying handlers are sync.

    Args:
        grouped_results: Dict mapping repo_id to list of result dicts
        search_type: Search type ('semantic', 'fts', 'temporal')

    Returns:
        Modified grouped_results with truncation applied to each result
    """
    from ..mcp.handlers import (
        _apply_payload_truncation,
        _apply_fts_payload_truncation,
        _apply_temporal_payload_truncation,
    )

    for repo_id, results in grouped_results.items():
        if search_type == "fts":
            grouped_results[repo_id] = _apply_fts_payload_truncation(results)
        elif search_type == "temporal":
            grouped_results[repo_id] = _apply_temporal_payload_truncation(results)
        else:
            # Default to semantic truncation (handles both content and code_snippet)
            grouped_results[repo_id] = _apply_payload_truncation(results)

    return grouped_results


# Create router with /api/query prefix
router = APIRouter(prefix="/api/query", tags=["multi-query"])

# Initialize multi-search service with default configuration
_multi_search_service: Optional[MultiSearchService] = None


def get_multi_search_service() -> MultiSearchService:
    """
    Get or create MultiSearchService instance.

    Uses ConfigService for configuration (Story #25) instead of environment variables.

    Returns:
        MultiSearchService instance
    """
    global _multi_search_service
    if _multi_search_service is None:
        from ..services.config_service import get_config_service

        config_service = get_config_service()
        config = MultiSearchConfig.from_config(config_service)
        from ..app import _server_hnsw_cache

        _multi_search_service = MultiSearchService(
            config, hnsw_index_cache=_server_hnsw_cache
        )
    return _multi_search_service


@router.post("/multi", response_model=MultiSearchResponse)
def multi_repository_query(
    request: MultiSearchRequest,
    *,
    http_request: Request,
    user: User = Depends(get_current_user),
) -> MultiSearchResponse:
    """
    Execute search across multiple repositories (AC1: REST Endpoint).

    Performs parallel search across specified repositories with:
    - Authentication enforcement (JWT token required)
    - Request validation (Pydantic models)
    - Timeout handling (30s default per repo)
    - Partial failure support (some repos succeed, others fail)
    - Result aggregation with repository attribution

    **Authentication**: Requires valid JWT token in Authorization header.

    **Request Body**:
    ```json
    {
        "repositories": ["repo1", "repo2"],
        "query": "authentication logic",
        "search_type": "semantic",
        "limit": 10,
        "min_score": 0.7,
        "language": "python",
        "path_filter": "*/src/*"
    }
    ```

    **Response Structure**:
    ```json
    {
        "results": {
            "repo1": [
                {
                    "file_path": "auth.py",
                    "line_start": 10,
                    "line_end": 20,
                    "score": 0.9,
                    "content": "def authenticate():",
                    "language": "python",
                    "repository": "repo1"
                }
            ],
            "repo2": [...]
        },
        "metadata": {
            "total_results": 15,
            "total_repos_searched": 2,
            "execution_time_ms": 250
        },
        "errors": {
            "repo3": "Query timeout after 30 seconds. Recommendations: ..."
        }
    }
    ```

    **Search Types**:
    - `semantic`: Vector similarity search (uses embeddings)
    - `fts`: Full-text search (Tantivy index)
    - `regex`: Regular expression search (subprocess isolation)
    - `temporal`: Git history search

    **Threading Strategy**:
    - Semantic/FTS/Temporal: ThreadPoolExecutor (max 10 workers)
    - Regex: Subprocess isolation (ReDoS protection)

    **Timeout Behavior**:
    - Each repository has 30s timeout (configurable via CIDX_MULTI_QUERY_TIMEOUT env var)
    - Timed out repos return error with actionable recommendations
    - Successful repos return results even if others time out

    **Error Handling**:
    - Repository not found → error in `errors` field, other repos succeed
    - Invalid query → 422 Unprocessable Entity
    - Authentication failure → 401 Unauthorized
    - Caller lacks access to a requested repo → 403 Forbidden
    - Unexpected error → 500 Internal Server Error

    Args:
        request: Multi-search request with repositories, query, and filters
        http_request: The real FastAPI Request (required, keyword-only) --
            used to resolve app.state.access_filtering_service for the
            repo-level access check below. FastAPI injects it for every
            genuine HTTP call automatically.
        user: Authenticated user (injected by dependency)

    Returns:
        MultiSearchResponse with results grouped by repository, metadata, and errors

    Raises:
        HTTPException: 401 if authentication fails
        HTTPException: 403 if the caller lacks access to a requested repo
        HTTPException: 422 if request validation fails
        HTTPException: 500 if unexpected error occurs
    """
    # ------------------------------------------------------------------
    # Repo-level access check — UNCONDITIONAL,
    # BEFORE any search execution, for every repository in the request
    # regardless of search_type. Never skipped: access_filtering_service
    # missing fails closed (500) inside _enforce_repo_access, it does not
    # bypass the check.
    # ------------------------------------------------------------------
    access_filtering_service = getattr(
        http_request.app.state, "access_filtering_service", None
    )
    _enforce_repo_access(access_filtering_service, user.username, request.repositories)

    # The SAME repository-count cap MCP omni search enforces (Bug #894,
    # multi_search_limits_config.omni_max_repos_per_search, read fresh so a
    # Web UI change applies at once): one fan-out bound for both doors.
    from ..mcp.handlers._utils import _enforce_repo_count_cap

    repo_count_breach = _enforce_repo_count_cap(request.repositories)
    if repo_count_breach is not None:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": repo_count_breach.error_code,
                "detail": (
                    f"Maximum {repo_count_breach.configured_cap} repositories "
                    f"per search, got {repo_count_breach.observed_count}"
                ),
            },
        )

    try:
        # Bug #350: Track REST API call in metrics
        api_metrics_service.increment_other_api_call(username=user.username)

        # Log request
        logger.info(
            f"Multi-repo search request from user {user.username}: "
            f"{len(request.repositories)} repos, type={request.search_type}"
        )

        # Get service instance
        service = get_multi_search_service()

        # Execute search
        response = service.search(request)

        # Story #683: Apply payload truncation to results
        # Story #50: Now sync call (underlying handlers are sync)
        response.results = _apply_multi_truncation(
            response.results, request.search_type
        )

        # Log response summary
        logger.info(
            f"Multi-repo search completed: {response.metadata.total_results} results "
            f"from {response.metadata.total_repos_searched} repos "
            f"in {response.metadata.execution_time_ms}ms"
        )

        return response

    except ValueError as e:
        # Validation error from service
        logger.error(
            format_error_log(
                "WEB-GENERAL-029", "Multi-repo search validation error", error=str(e)
            ),
            extra=get_log_extra("WEB-GENERAL-029"),
        )
        raise HTTPException(status_code=422, detail=str(e))

    except Exception as e:
        # Unexpected error
        logger.error(
            format_error_log(
                "WEB-GENERAL-030", "Multi-repo search failed", error=str(e)
            ),
            extra=get_log_extra("WEB-GENERAL-030"),
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=f"Multi-repository search failed: {str(e)}"
        )


# Cleanup function for service shutdown
def shutdown_multi_search_service():
    """Shutdown multi-search service and clean up resources."""
    global _multi_search_service
    if _multi_search_service:
        _multi_search_service.shutdown()
        _multi_search_service = None
        logger.info("Multi-search service shutdown complete")
