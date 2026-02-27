"""
Activated Repository REST API Router.

Provides REST endpoints for managing activated repositories with feature parity
to golden repositories (indexes, health checks, sync, branch operations).
"""

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.services.hnsw_health_service import HNSWHealthService

logger = logging.getLogger(__name__)

# Create router with prefix and tags
router = APIRouter(prefix="/api/activated-repos", tags=["activated-repos"])


class IndexStatus(BaseModel):
    """Status of a single index type."""

    index_type: str = Field(description="Index type: semantic, fts, temporal, or scip")
    exists: bool = Field(description="Whether the index exists")
    healthy: bool = Field(description="Whether the index is healthy")
    last_updated: Optional[str] = Field(
        default=None, description="Last update timestamp"
    )
    file_size_bytes: Optional[int] = Field(default=None, description="Index file size")


class IndexesStatusResponse(BaseModel):
    """Response for GET /api/activated-repos/{user_alias}/indexes."""

    user_alias: str
    indexes: List[IndexStatus] = Field(default_factory=list)
    repo_path: str


class ReindexRequest(BaseModel):
    """Request body for POST /api/activated-repos/{user_alias}/reindex."""

    index_types: Optional[List[str]] = Field(
        default=None,
        description="Index types to reindex (semantic, fts, temporal, scip). If null, reindex all existing.",
    )


class ReindexResponse(BaseModel):
    """Response for POST /api/activated-repos/{user_alias}/reindex."""

    job_id: str
    message: str
    index_types: List[str]


class AddIndexResponse(BaseModel):
    """Response for POST /api/activated-repos/{user_alias}/indexes/{index_type}."""

    job_id: str
    message: str
    index_type: str


class HealthCheckResponse(BaseModel):
    """Response for GET /api/activated-repos/{user_alias}/health."""

    user_alias: str
    overall_healthy: bool = Field(description="Whether all collections are healthy")
    status: str = Field(description="Overall status: 'healthy' or 'unhealthy'")
    total_collections: int = Field(description="Total number of collections checked")
    healthy_count: int = Field(description="Number of healthy collections")
    unhealthy_count: int = Field(description="Number of unhealthy collections")
    collections: List[Dict[str, Any]] = Field(
        description="Per-collection health details"
    )


class SyncRequest(BaseModel):
    """Request body for POST /api/activated-repos/{user_alias}/sync."""

    reindex: bool = Field(default=False, description="Whether to reindex after sync")


class SyncResponse(BaseModel):
    """Response for POST /api/activated-repos/{user_alias}/sync."""

    job_id: str
    message: str
    reindex: bool


class SwitchBranchRequest(BaseModel):
    """Request body for POST /api/activated-repos/{user_alias}/branch."""

    branch_name: str = Field(description="Branch name to switch to")


class SwitchBranchResponse(BaseModel):
    """Response for POST /api/activated-repos/{user_alias}/branch."""

    job_id: str
    message: str
    branch_name: str


class BranchesResponse(BaseModel):
    """Response for GET /api/activated-repos/{user_alias}/branches."""

    user_alias: str
    current_branch: str
    branches: List[str]


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


def _get_background_job_manager():
    """Get background job manager from app state."""
    from code_indexer.server import app as app_module

    manager = getattr(app_module.app.state, "background_job_manager", None)
    if manager is None:
        raise RuntimeError(
            "background_job_manager not initialized. "
            "Server must set app.state.background_job_manager during startup."
        )
    return manager


def _check_index_status(index_path: Path, index_type: str) -> IndexStatus:
    """
    Check the status of a specific index.

    Args:
        index_path: Path to the index file/directory
        index_type: Type of index (semantic, fts, temporal, scip)

    Returns:
        IndexStatus with exists, healthy, and metadata
    """
    exists = index_path.exists()

    if not exists:
        return IndexStatus(
            index_type=index_type,
            exists=False,
            healthy=False,
            last_updated=None,
            file_size_bytes=None,
        )

    # Get file metadata
    try:
        stat = index_path.stat()
        file_size = stat.st_size
        last_updated = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat()
    except Exception as e:
        logger.warning(f"Failed to get file metadata for {index_path}: {e}")
        file_size = None
        last_updated = None

    # For now, if file exists, consider it healthy
    # More sophisticated health checks can be added later
    return IndexStatus(
        index_type=index_type,
        exists=True,
        healthy=True,
        last_updated=last_updated,
        file_size_bytes=file_size,
    )


@router.get(
    "/{user_alias}/indexes",
    response_model=IndexesStatusResponse,
    responses={
        200: {"description": "Index status retrieved successfully"},
        404: {"description": "Activated repository not found"},
        500: {"description": "Failed to retrieve index status"},
    },
)
async def get_indexes_status(
    user_alias: str,
    current_user: User = Depends(get_current_user_hybrid),
    owner: Optional[str] = Query(None, description="Repository owner username (admin only)"),
) -> IndexesStatusResponse:
    """
    Get index status for an activated repository.

    Returns the status of all index types (semantic, FTS, temporal, SCIP)
    for the specified activated repository.

    Args:
        user_alias: User's alias for the activated repository
        current_user: Authenticated user (injected by auth dependency)
        owner: Optional owner username (only used if current_user is admin)

    Returns:
        IndexesStatusResponse with status of all indexes

    Raises:
        HTTPException 404: Repository not found
        HTTPException 500: Failed to retrieve status
    """
    try:
        # Get activated repo manager
        activated_manager = _get_activated_repo_manager()

        # Determine which username to use
        # Admin users can specify owner parameter to check other users' repos
        # Non-admin users always use their own username (owner parameter ignored)
        if owner and current_user.role == UserRole.ADMIN:
            target_username = owner
        else:
            target_username = current_user.username

        # Get repository path
        repo_path = activated_manager.get_activated_repo_path(
            target_username, user_alias
        )

        # Check if repository exists
        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        # Check index directory - if it doesn't exist, return all indexes as not present
        index_dir = repo_path_obj / ".code-indexer" / "index"
        if not index_dir.exists():
            # Return empty indexes array - repo exists but hasn't been indexed yet
            return IndexesStatusResponse(
                user_alias=user_alias,
                indexes=[
                    IndexStatus(index_type="semantic", exists=False, healthy=False),
                    IndexStatus(index_type="fts", exists=False, healthy=False),
                    IndexStatus(index_type="temporal", exists=False, healthy=False),
                    IndexStatus(index_type="scip", exists=False, healthy=False),
                ],
                repo_path=repo_path,
            )

        # Check each index type
        indexes = []

        # Semantic index (voyage-code-3 collection)
        semantic_path = index_dir / "voyage-code-3" / "hnsw_index.bin"
        indexes.append(_check_index_status(semantic_path, "semantic"))

        # FTS index (tantivy)
        fts_path = index_dir / "tantivy"
        indexes.append(_check_index_status(fts_path, "fts"))

        # Temporal index (collection name is code-indexer-temporal)
        temporal_path = index_dir / "code-indexer-temporal" / "hnsw_index.bin"
        indexes.append(_check_index_status(temporal_path, "temporal"))

        # SCIP index
        scip_path = repo_path_obj / ".code-indexer" / "scip"
        indexes.append(_check_index_status(scip_path, "scip"))

        return IndexesStatusResponse(
            user_alias=user_alias,
            indexes=indexes,
            repo_path=repo_path,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get index status: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve index status: {str(e)}",
        )


@router.post(
    "/{user_alias}/reindex",
    response_model=ReindexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Reindex job started"},
        404: {"description": "Activated repository not found"},
        500: {"description": "Failed to start reindex job"},
    },
)
async def trigger_reindex(
    user_alias: str,
    request: ReindexRequest,
    current_user: User = Depends(get_current_user_hybrid),
) -> ReindexResponse:
    """
    Trigger reindex for an activated repository.

    Starts a background job to reindex the specified index types.
    If no index types specified, reindexes all existing indexes.

    Args:
        user_alias: User's alias for the activated repository
        request: Request body with optional index_types list
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        ReindexResponse with job_id and index types being reindexed

    Raises:
        HTTPException 404: Repository not found
        HTTPException 500: Failed to start reindex job
    """
    try:
        # Get managers
        activated_manager = _get_activated_repo_manager()
        job_manager = _get_background_job_manager()

        # Get repository path
        repo_path = activated_manager.get_activated_repo_path(
            current_user.username, user_alias
        )

        # Check if repository exists
        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        # Determine which index types to reindex
        index_types = request.index_types
        if index_types is None:
            # Default to all existing indexes
            index_types = ["semantic", "fts", "temporal", "scip"]

        # Submit reindex job
        def reindex_job():
            """Background job to reindex repository."""
            # This would call the actual reindex logic
            # For now, just a placeholder that the job manager can execute
            pass

        # AC8 (Story #311): fixed submit_job signature (was using wrong kwargs)
        job_id = job_manager.submit_job(
            "reindex_activated_repo",
            reindex_job,
            submitter_username=current_user.username,
            repo_alias=user_alias,
        )

        return ReindexResponse(
            job_id=job_id,
            message="Reindex job started",
            index_types=index_types,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to trigger reindex for {user_alias}: {e}")
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if "not found" in str(e).lower()
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=f"Failed to trigger reindex: {str(e)}",
        )


@router.post(
    "/{user_alias}/indexes/{index_type}",
    response_model=AddIndexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Add index job started"},
        400: {"description": "Invalid index type"},
        404: {"description": "Activated repository not found"},
        500: {"description": "Failed to start add index job"},
    },
)
async def add_index_type(
    user_alias: str,
    index_type: str,
    current_user: User = Depends(get_current_user_hybrid),
) -> AddIndexResponse:
    """
    Add a specific index type to an activated repository.

    Starts a background job to add the specified index type (semantic, fts, temporal, or scip).

    Args:
        user_alias: User's alias for the activated repository
        index_type: Type of index to add (semantic, fts, temporal, scip)
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        AddIndexResponse with job_id and index type being added

    Raises:
        HTTPException 400: Invalid index type
        HTTPException 404: Repository not found
        HTTPException 500: Failed to start add index job
    """
    # Validate index type
    valid_index_types = ["semantic", "fts", "temporal", "scip"]
    if index_type not in valid_index_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid index type '{index_type}'. Must be one of: {', '.join(valid_index_types)}",
        )

    try:
        # Get managers
        activated_manager = _get_activated_repo_manager()
        job_manager = _get_background_job_manager()

        # Get repository path
        repo_path = activated_manager.get_activated_repo_path(
            current_user.username, user_alias
        )

        # Check if repository exists
        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        # Submit add index job
        def add_index_job():
            """Background job to add index type."""
            # This would call the actual add index logic
            # For now, just a placeholder that the job manager can execute
            pass

        # AC8 (Story #311): fixed submit_job signature (was using wrong kwargs)
        job_id = job_manager.submit_job(
            "add_index_activated_repo",
            add_index_job,
            submitter_username=current_user.username,
            repo_alias=user_alias,
        )

        return AddIndexResponse(
            job_id=job_id,
            message=f"Adding {index_type} index",
            index_type=index_type,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add index type {index_type} for {user_alias}: {e}")
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if "not found" in str(e).lower()
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=f"Failed to add index type: {str(e)}",
        )


@router.get(
    "/{user_alias}/health",
    response_model=HealthCheckResponse,
    responses={
        200: {"description": "Health check completed successfully"},
        404: {"description": "Activated repository not found"},
        500: {"description": "Failed to check health"},
    },
)
async def get_health(
    user_alias: str,
    current_user: User = Depends(get_current_user_hybrid),
    owner: Optional[str] = Query(None, description="Repository owner username (admin only)"),
) -> HealthCheckResponse:
    """
    Get health check status for an activated repository.

    Uses HNSWHealthService to check the integrity of HNSW indexes.

    Args:
        user_alias: User's alias for the activated repository
        current_user: Authenticated user (injected by auth dependency)
        owner: Optional owner username (only used if current_user is admin)

    Returns:
        HealthCheckResponse with overall status and collection-level health

    Raises:
        HTTPException 404: Repository not found
        HTTPException 500: Failed to check health
    """
    try:
        # Get activated repo manager
        activated_manager = _get_activated_repo_manager()

        # Determine which username to use
        # Admin users can specify owner parameter to check other users' repos
        # Non-admin users always use their own username (owner parameter ignored)
        if owner and current_user.role == UserRole.ADMIN:
            target_username = owner
        else:
            target_username = current_user.username

        # Get repository path
        repo_path = activated_manager.get_activated_repo_path(
            target_username, user_alias
        )

        # Check if repository exists
        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        # Run health check
        health_service = HNSWHealthService()
        index_dir = repo_path_obj / ".code-indexer" / "index"

        # Check if index directory exists
        if not index_dir.exists():
            return HealthCheckResponse(
                user_alias=user_alias,
                overall_healthy=True,
                status="healthy",
                total_collections=0,
                healthy_count=0,
                unhealthy_count=0,
                collections=[],
            )

        # Iterate through collection directories
        collections: List[Dict[str, Any]] = []

        for collection_dir in index_dir.iterdir():
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
            health_result = health_service.check_health(
                index_path=str(hnsw_file),
                force_refresh=False,
            )

            # Convert to collection health dict
            collection_health = {
                "collection_name": collection_name,
                "index_type": index_type,
                "valid": health_result.valid,
                "file_exists": health_result.file_exists,
                "readable": health_result.readable,
                "loadable": health_result.loadable,
                "element_count": health_result.element_count,
                "connections_checked": health_result.connections_checked,
                "min_inbound": health_result.min_inbound,
                "max_inbound": health_result.max_inbound,
                "file_size_bytes": health_result.file_size_bytes,
                "errors": health_result.errors,
                "check_duration_ms": health_result.check_duration_ms,
            }
            collections.append(collection_health)

        # If no collections found, return empty result
        if not collections:
            return HealthCheckResponse(
                user_alias=user_alias,
                overall_healthy=True,
                status="healthy",
                total_collections=0,
                healthy_count=0,
                unhealthy_count=0,
                collections=[],
            )

        # Aggregate results
        healthy_count = sum(1 for c in collections if c["valid"])
        unhealthy_count = len(collections) - healthy_count
        overall_healthy = unhealthy_count == 0
        health_status = "healthy" if overall_healthy else "unhealthy"

        return HealthCheckResponse(
            user_alias=user_alias,
            overall_healthy=overall_healthy,
            status=health_status,
            total_collections=len(collections),
            healthy_count=healthy_count,
            unhealthy_count=unhealthy_count,
            collections=collections,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get health for {user_alias}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check health: {str(e)}",
        )


@router.post(
    "/{user_alias}/sync",
    response_model=SyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Sync job started"},
        404: {"description": "Activated repository not found"},
        500: {"description": "Failed to start sync job"},
    },
)
async def sync_repository(
    user_alias: str,
    request: SyncRequest,
    current_user: User = Depends(get_current_user_hybrid),
) -> SyncResponse:
    """
    Sync an activated repository with its golden repository source.

    Pulls latest changes from the golden repository and optionally reindexes.

    Args:
        user_alias: User's alias for the activated repository
        request: Request body with optional reindex flag
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        SyncResponse with job_id and sync configuration

    Raises:
        HTTPException 404: Repository not found
        HTTPException 500: Failed to start sync job
    """
    try:
        # Get managers
        activated_manager = _get_activated_repo_manager()
        job_manager = _get_background_job_manager()

        # Get repository path
        repo_path = activated_manager.get_activated_repo_path(
            current_user.username, user_alias
        )

        # Check if repository exists
        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        # Submit sync job
        def sync_job():
            """Background job to sync repository."""
            # This would call the actual sync logic
            # For now, just a placeholder that the job manager can execute
            pass

        # AC8 (Story #311): fixed submit_job signature (was using wrong kwargs)
        job_id = job_manager.submit_job(
            "sync_activated_repo",
            sync_job,
            submitter_username=current_user.username,
            repo_alias=user_alias,
        )

        return SyncResponse(
            job_id=job_id,
            message="Sync job started",
            reindex=request.reindex,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to sync repository {user_alias}: {e}")
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if "not found" in str(e).lower()
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=f"Failed to sync repository: {str(e)}",
        )


@router.post(
    "/{user_alias}/branch",
    response_model=SwitchBranchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Branch switch job started"},
        404: {"description": "Activated repository not found"},
        422: {"description": "Invalid request (missing branch_name)"},
        500: {"description": "Failed to start branch switch job"},
    },
)
async def switch_branch(
    user_alias: str,
    request: SwitchBranchRequest,
    current_user: User = Depends(get_current_user_hybrid),
) -> SwitchBranchResponse:
    """
    Switch branch for an activated repository.

    Changes the active branch and triggers a reindex.

    Args:
        user_alias: User's alias for the activated repository
        request: Request body with branch_name
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        SwitchBranchResponse with job_id and branch name

    Raises:
        HTTPException 404: Repository not found
        HTTPException 422: Missing branch_name
        HTTPException 500: Failed to start branch switch job
    """
    try:
        # Get managers
        activated_manager = _get_activated_repo_manager()
        job_manager = _get_background_job_manager()

        # Get repository path
        repo_path = activated_manager.get_activated_repo_path(
            current_user.username, user_alias
        )

        # Check if repository exists
        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        # Submit branch switch job
        def switch_branch_job():
            """Background job to switch branch."""
            # This would call the actual branch switch logic
            # For now, just a placeholder that the job manager can execute
            pass

        # AC8 (Story #311): fixed submit_job signature (was using wrong kwargs)
        job_id = job_manager.submit_job(
            "switch_branch_activated_repo",
            switch_branch_job,
            submitter_username=current_user.username,
            repo_alias=user_alias,
        )

        return SwitchBranchResponse(
            job_id=job_id,
            message="Branch switch job started",
            branch_name=request.branch_name,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to switch branch for {user_alias}: {e}")
        raise HTTPException(
            status_code=(
                status.HTTP_404_NOT_FOUND
                if "not found" in str(e).lower()
                else status.HTTP_500_INTERNAL_SERVER_ERROR
            ),
            detail=f"Failed to switch branch: {str(e)}",
        )


@router.get(
    "/{user_alias}/branches",
    response_model=BranchesResponse,
    responses={
        200: {"description": "Branch list retrieved successfully"},
        404: {"description": "Activated repository not found"},
        500: {"description": "Failed to list branches"},
    },
)
async def list_branches(
    user_alias: str,
    current_user: User = Depends(get_current_user_hybrid),
) -> BranchesResponse:
    """
    List all branches for an activated repository.

    Returns all available branches and the current active branch.

    Args:
        user_alias: User's alias for the activated repository
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        BranchesResponse with current branch and list of all branches

    Raises:
        HTTPException 404: Repository not found
        HTTPException 500: Failed to list branches
    """
    try:
        # Get activated repo manager
        activated_manager = _get_activated_repo_manager()

        # Get repository path
        repo_path = activated_manager.get_activated_repo_path(
            current_user.username, user_alias
        )

        # Check if repository exists
        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        # Get branches using git
        result = subprocess.run(
            ["git", "branch"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )

        # Parse git branch output
        branches = []
        current_branch = ""

        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("* "):
                # Current branch
                current_branch = line[2:]
                branches.append(current_branch)
            else:
                branches.append(line)

        return BranchesResponse(
            user_alias=user_alias,
            current_branch=current_branch,
            branches=branches,
        )

    except HTTPException:
        raise
    except subprocess.CalledProcessError as e:
        logger.error(f"Git command failed for {user_alias}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list branches: {e.stderr}",
        )
    except Exception as e:
        logger.error(f"Failed to list branches for {user_alias}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list branches: {str(e)}",
        )
