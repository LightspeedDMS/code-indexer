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
from ..multi.models import MultiSearchMetadata
from ..services.shard_router import FORWARD_HEADER
from code_indexer.server.services.api_metrics_service import api_metrics_service

logger = logging.getLogger(__name__)


def _merge_multi_responses(parts: List[Dict[str, Any]]) -> MultiSearchResponse:
    """Merge MultiSearchResponse-shaped dicts from local + forwarded searches.

    Results are grouped by repository and limited per-repo, so merging across
    pods is a plain dict union -- no global re-ranking needed.
    """
    results: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    total_repos = 0
    exec_ms = 0
    for part in parts:
        results.update(part.get("results") or {})
        errors.update(part.get("errors") or {})
        meta = part.get("metadata") or {}
        total_repos += int(meta.get("total_repos_searched") or 0)
        exec_ms = max(exec_ms, int(meta.get("execution_time_ms") or 0))
    total_results = sum(len(v) for v in results.values())
    return MultiSearchResponse(
        results=results,
        metadata=MultiSearchMetadata(
            total_results=total_results,
            total_repos_searched=total_repos,
            execution_time_ms=exec_ms,
        ),
        errors=errors or None,
    )


def _scatter_gather(
    shard_router: Any, request: MultiSearchRequest, auth_header: Optional[str]
) -> MultiSearchResponse:
    """C6 Phase 2: split repositories by owner, search local repos locally and
    forward each remote group to its owning pod, then merge the responses.

    Distributes the fan-out across pods (each owner searches only its shard)
    instead of one pod loading every repo. Fails open per-group: a failed
    forward falls back to searching those repos locally.
    """
    local_repos, groups = shard_router.group_by_owner(request.repositories)
    parts: List[Dict[str, Any]] = []
    for address, repos in groups.items():
        sub = request.model_copy(update={"repositories": repos})
        try:
            parts.append(
                shard_router.forward(
                    address, sub.model_dump(), auth_header, path="/api/query/multi"
                )
            )
        except Exception as exc:
            logger.warning(
                "C6: multi forward to %s failed (%s); searching %d repos locally",
                address,
                exc,
                len(repos),
            )
            local_repos = local_repos + repos
    if local_repos:
        sub = request.model_copy(update={"repositories": local_repos})
        parts.append(get_multi_search_service().search(sub).model_dump())
    return _merge_multi_responses(parts)


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
        _multi_search_service = MultiSearchService(config)
    return _multi_search_service


@router.post("/multi", response_model=MultiSearchResponse)
def multi_repository_query(
    request: MultiSearchRequest,
    raw_request: Request,
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
    - Unexpected error → 500 Internal Server Error

    Args:
        request: Multi-search request with repositories, query, and filters
        user: Authenticated user (injected by dependency)

    Returns:
        MultiSearchResponse with results grouped by repository, metadata, and errors

    Raises:
        HTTPException: 401 if authentication fails
        HTTPException: 422 if request validation fails
        HTTPException: 500 if unexpected error occurs
    """
    try:
        # Bug #350: Track REST API call in metrics
        api_metrics_service.increment_other_api_call(username=user.username)

        # Log request
        logger.info(
            f"Multi-repo search request from user {user.username}: "
            f"{len(request.repositories)} repos, type={request.search_type}"
        )

        # C6 Phase 2: scatter-gather across owning pods for multi-repo queries.
        # Split the repositories by owner, search local ones locally and forward
        # each remote group to its owning pod, then merge. A forwarded sub-request
        # (loop-guard header) is served locally here instead of scattering again.
        shard_router = getattr(raw_request.app.state, "shard_router", None)
        # C6 Phase 3: let the multi-search service warm this pod's index cache for
        # repos it owns (idempotent per-request injection). No-op in solo mode.
        get_multi_search_service().set_shard_ownership(
            getattr(raw_request.app.state, "shard_ownership", None)
        )
        forwarded = raw_request.headers.get(FORWARD_HEADER.lower()) == "1"
        if shard_router is not None and not forwarded and request.repositories:
            try:
                response = _scatter_gather(
                    shard_router,
                    request,
                    raw_request.headers.get("authorization"),
                )
            except Exception:
                logger.warning(
                    "C6: multi scatter-gather failed; searching all repos locally",
                    extra=get_log_extra("WEB-GENERAL-030"),
                )
                response = get_multi_search_service().search(request)
        else:
            response = get_multi_search_service().search(request)

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
