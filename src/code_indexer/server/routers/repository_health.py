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


class IndexesStatusResponse(BaseModel):
    """Index availability status for a repository."""

    has_semantic: bool = Field(description="Semantic index available")
    has_fts: bool = Field(description="Full-text search index available")
    has_temporal: bool = Field(description="Temporal (git history) index available")
    has_scip: bool = Field(description="SCIP code intelligence index available")


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


def _get_activated_repo_manager():
    """Get activated repository manager from app state."""
    from code_indexer.server import app as app_module

    manager = getattr(app_module.app.state, "activated_repo_manager", None)
    if manager is None:
        raise RuntimeError(
            "activated_repo_manager not initialized. "
            "Server must set app.state.activated_repo_manager during startup."
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
        # Multi-strategy repository resolution (Story #58)
        # Strategy 1: Try as golden repo (exact match)
        golden_repo_manager = _get_golden_repo_manager()
        repo = golden_repo_manager.get_golden_repo(repo_alias)
        repo_path = None
        resolved_alias = repo_alias

        # Strategy 2: If not found and ends with -global, try without suffix
        if not repo and repo_alias.endswith("-global"):
            base_alias = repo_alias[:-7]  # Remove "-global" suffix
            repo = golden_repo_manager.get_golden_repo(base_alias)
            if repo:
                resolved_alias = base_alias

        # Strategy 3: Try as user-activated repo
        if not repo:
            activated_repo_manager = _get_activated_repo_manager()
            potential_path = activated_repo_manager.get_activated_repo_path(
                current_user.username, repo_alias
            )
            # Validate path exists before using it
            if Path(potential_path).exists():
                repo_path = potential_path

        # If still not found via any strategy, return 404
        if not repo and not repo_path:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{repo_alias}' not found",
            )

        # Resolve index path using actual filesystem path
        if repo:
            # Golden repo path
            actual_path = golden_repo_manager.get_actual_repo_path(resolved_alias)
            clone_path = Path(actual_path)
        else:
            # Activated repo path
            clone_path = Path(repo_path)
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


@router.get(
    "/{repo_alias}/indexes",
    response_model=IndexesStatusResponse,
    responses={
        200: {"description": "Index status retrieved successfully"},
        404: {"description": "Repository not found"},
        500: {"description": "Failed to check index status"},
    },
)
async def get_repository_indexes(
    repo_alias: str,
    current_user: User = Depends(get_current_user_hybrid),
) -> IndexesStatusResponse:
    """
    Get index availability status for a repository.

    Checks for the presence of semantic, FTS, temporal, and SCIP indexes
    in the repository using the same multi-strategy resolution as the health endpoint.

    Index detection logic:
    - Semantic: {repo_path}/.code-indexer/index/voyage-code-3/hnsw_index.bin exists
    - FTS: {repo_path}/.code-indexer/index/tantivy/ directory exists
    - Temporal: {repo_path}/.code-indexer/index/temporal/ OR
                {repo_path}/.code-indexer/index/code-indexer-temporal/ directory exists
                with hnsw_index.bin
    - SCIP: {repo_path}/.code-indexer/scip/ directory exists with .scip.db files

    Resolution strategy:
    1. Try as golden repo (exact match)
    2. If ends with -global, try without suffix
    3. Try as user-activated repo

    Args:
        repo_alias: Repository alias (e.g., 'backend', 'python-mock-global')
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        IndexesStatusResponse with boolean flags for each index type

    Raises:
        HTTPException 404: Repository not found
        HTTPException 500: Failed to check index status
    """
    try:
        # Multi-strategy repository resolution (same as health endpoint)
        # Strategy 1: Try as golden repo (exact match)
        golden_repo_manager = _get_golden_repo_manager()
        repo = golden_repo_manager.get_golden_repo(repo_alias)
        repo_path = None
        resolved_alias = repo_alias

        # Strategy 2: If not found and ends with -global, try without suffix
        if not repo and repo_alias.endswith("-global"):
            base_alias = repo_alias[:-7]  # Remove "-global" suffix
            repo = golden_repo_manager.get_golden_repo(base_alias)
            if repo:
                resolved_alias = base_alias

        # Strategy 3: Try as user-activated repo
        if not repo:
            activated_repo_manager = _get_activated_repo_manager()
            potential_path = activated_repo_manager.get_activated_repo_path(
                current_user.username, repo_alias
            )
            # Validate path exists before using it
            if Path(potential_path).exists():
                repo_path = potential_path

        # If still not found via any strategy, return 404
        if not repo and not repo_path:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{repo_alias}' not found",
            )

        # Resolve actual filesystem path
        if repo:
            # Golden repo path
            actual_path = golden_repo_manager.get_actual_repo_path(resolved_alias)
            clone_path = Path(actual_path)
        else:
            # Activated repo path
            clone_path = Path(repo_path)

        # Check for each index type
        index_base_path = clone_path / ".code-indexer" / "index"

        # Semantic index: voyage-code-3/hnsw_index.bin
        semantic_path = index_base_path / "voyage-code-3" / "hnsw_index.bin"
        has_semantic = semantic_path.exists() and semantic_path.is_file()

        # FTS index: tantivy/ directory
        fts_path = index_base_path / "tantivy"
        has_fts = fts_path.exists() and fts_path.is_dir()

        # Temporal index: temporal/ OR code-indexer-temporal/ directory with hnsw_index.bin
        temporal_path1 = index_base_path / "temporal"
        temporal_path2 = index_base_path / "code-indexer-temporal"
        has_temporal = False

        if temporal_path1.exists() and temporal_path1.is_dir():
            temporal_bin = temporal_path1 / "hnsw_index.bin"
            has_temporal = temporal_bin.exists() and temporal_bin.is_file()
        elif temporal_path2.exists() and temporal_path2.is_dir():
            temporal_bin = temporal_path2 / "hnsw_index.bin"
            has_temporal = temporal_bin.exists() and temporal_bin.is_file()

        # SCIP index: scip/ directory with .scip.db files
        scip_path = clone_path / ".code-indexer" / "scip"
        has_scip = False
        if scip_path.exists() and scip_path.is_dir():
            # Check if there are any .scip.db files
            scip_files = list(scip_path.glob("*.scip.db"))
            has_scip = len(scip_files) > 0

        return IndexesStatusResponse(
            has_semantic=has_semantic,
            has_fts=has_fts,
            has_temporal=has_temporal,
            has_scip=has_scip,
        )

    except HTTPException:
        # Re-raise HTTP exceptions (404, etc.)
        raise
    except Exception as e:
        logger.error(
            f"Failed to check indexes for repository {repo_alias}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check index status: {str(e)}",
        )
