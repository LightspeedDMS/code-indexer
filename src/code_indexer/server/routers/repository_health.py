"""
Repository Health REST API Router.

Provides REST endpoints for checking HNSW index health with caching support.
"""

import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User
from code_indexer.services.hnsw_health_service import HNSWHealthService

logger = logging.getLogger(__name__)


class CollectionHealthResult(BaseModel):
    """Health result for a single collection/index."""

    collection_name: str = Field(description="Collection name (e.g., voyage-code-3)")
    index_type: str = Field(
        description="Index type: semantic, temporal, or multimodal"
    )
    valid: bool = Field(description="Overall health status")
    file_exists: bool
    readable: bool
    loadable: bool
    element_count: Optional[int] = None
    connections_checked: Optional[int] = None
    min_inbound: Optional[int] = None
    max_inbound: Optional[int] = None
    file_size_bytes: Optional[int] = None
    errors: List[str] = Field(default_factory=list)
    check_duration_ms: float


class RepositoryHealthResult(BaseModel):
    """Aggregated health for all indexes in a repository."""

    repo_alias: str
    overall_healthy: bool = Field(description="True if ALL indexes are healthy")
    collections: List[CollectionHealthResult] = Field(default_factory=list)
    total_collections: int = 0
    healthy_count: int = 0
    unhealthy_count: int = 0
    from_cache: bool = False


# Create router with prefix and tags
router = APIRouter(prefix="/api/repositories", tags=["repository-health"])

# Service instance (singleton pattern)
_health_service = HNSWHealthService(cache_ttl_seconds=300)  # 5 minute cache


def _get_golden_repo_manager():
    """Get golden repository manager from app state."""
    from code_indexer.server import app as app_module

    manager = getattr(app_module.app.state, "golden_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "golden_repo_manager not initialized. "
            "Server must set app.state.golden_repo_manager during startup."
        )
    return manager


@router.get(
    "/{repo_alias}/health",
    response_model=RepositoryHealthResult,
    responses={
        200: {"description": "Health check completed successfully"},
        404: {"description": "Repository not found"},
        500: {"description": "Health check failed"},
    },
)
async def get_repository_health(
    repo_alias: str,
    force_refresh: bool = Query(
        default=False, description="Bypass cache and perform fresh check"
    ),
    current_user: User = Depends(get_current_user_hybrid),
) -> RepositoryHealthResult:
    """
    Get HNSW index health for all collections in a repository.

    Performs comprehensive health check on ALL HNSW indexes (semantic, temporal,
    multimodal) in the repository, aggregating results.

    For each collection:
    - File existence and readability
    - HNSW loadability
    - Integrity validation (connections, inbound links)
    - File metadata (size, modification time)

    Results are cached for 5 minutes unless force_refresh=true.

    Args:
        repo_alias: Repository alias (e.g., 'backend', 'frontend')
        force_refresh: If True, bypass cache and perform fresh check
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        RepositoryHealthResult with per-collection health and aggregated status

    Raises:
        HTTPException 404: Repository not found
        HTTPException 500: Health check failed unexpectedly
    """
    try:
        # Resolve repository alias to clone path
        golden_repo_manager = _get_golden_repo_manager()
        repo = golden_repo_manager.get_golden_repo(repo_alias)

        if not repo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository not found: {repo_alias}",
            )

        # Resolve index path
        clone_path = Path(repo.clone_path)
        index_base_path = clone_path / ".code-indexer" / "index"

        # Check if index directory exists
        if not index_base_path.exists() or not index_base_path.is_dir():
            # No collections yet - return empty result with overall_healthy=True
            return RepositoryHealthResult(
                repo_alias=repo_alias,
                overall_healthy=True,
                collections=[],
                total_collections=0,
                healthy_count=0,
                unhealthy_count=0,
                from_cache=False,
            )

        # Iterate all collection directories
        collections: List[CollectionHealthResult] = []
        any_from_cache = False

        for collection_dir in index_base_path.iterdir():
            if not collection_dir.is_dir():
                continue

            hnsw_file = collection_dir / "hnsw_index.bin"
            if not hnsw_file.exists():
                continue

            collection_name = collection_dir.name

            # Determine index type from collection name
            if "temporal" in collection_name.lower():
                index_type = "temporal"
            elif "multimodal" in collection_name.lower():
                index_type = "multimodal"
            else:
                index_type = "semantic"

            # Perform health check for this collection
            health_result = _health_service.check_health(
                index_path=str(hnsw_file),
                force_refresh=force_refresh,
            )

            # Track if any result came from cache
            if health_result.from_cache:
                any_from_cache = True

            # Convert to CollectionHealthResult
            collection_health = CollectionHealthResult(
                collection_name=collection_name,
                index_type=index_type,
                valid=health_result.valid,
                file_exists=health_result.file_exists,
                readable=health_result.readable,
                loadable=health_result.loadable,
                element_count=health_result.element_count,
                connections_checked=health_result.connections_checked,
                min_inbound=health_result.min_inbound,
                max_inbound=health_result.max_inbound,
                file_size_bytes=health_result.file_size_bytes,
                errors=health_result.errors,
                check_duration_ms=health_result.check_duration_ms,
            )

            collections.append(collection_health)

        # If no collections found, return empty result
        if not collections:
            return RepositoryHealthResult(
                repo_alias=repo_alias,
                overall_healthy=True,
                collections=[],
                total_collections=0,
                healthy_count=0,
                unhealthy_count=0,
                from_cache=False,
            )

        # Aggregate results
        healthy_count = sum(1 for c in collections if c.valid)
        unhealthy_count = len(collections) - healthy_count
        overall_healthy = unhealthy_count == 0

        return RepositoryHealthResult(
            repo_alias=repo_alias,
            overall_healthy=overall_healthy,
            collections=collections,
            total_collections=len(collections),
            healthy_count=healthy_count,
            unhealthy_count=unhealthy_count,
            from_cache=any_from_cache,
        )

    except HTTPException:
        # Re-raise HTTP exceptions (404, etc.)
        raise
    except Exception as e:
        logger.error(
            f"Health check failed for repository {repo_alias}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Health check failed: {str(e)}",
        )
