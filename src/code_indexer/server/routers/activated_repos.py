"""
Activated Repository REST API Router.

Provides REST endpoints for managing activated repositories with feature parity
to golden repositories (indexes, health checks, sync, branch operations).
"""

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.repository_health_aggregator import (
    compute_repository_health,
    get_shared_health_service,
)

logger = logging.getLogger(__name__)


def resolve_semantic_index_path(index_dir: Path) -> Path:
    """Resolve the path to the semantic hnsw_index.bin for any embedding provider.

    Scans deterministically for any collection with hnsw_index.bin that is not
    a multimodal, temporal, or tantivy collection, excluding voyage-code-3 as a
    special candidate (used only as fallback when no other provider is found).

    Args:
        index_dir: Path to .code-indexer/index directory.

    Returns:
        Path to the semantic hnsw_index.bin (may not exist if not yet indexed).
    """
    fallback = index_dir / "voyage-code-3" / "hnsw_index.bin"
    if not (index_dir.exists() and index_dir.is_dir()):
        return fallback
    for subdir in sorted(index_dir.iterdir(), key=lambda p: p.name):
        if not subdir.is_dir():
            continue
        name = subdir.name
        if "multimodal" in name or "temporal" in name or "tantivy" in name:
            continue
        if name == "voyage-code-3":
            continue  # treat as fallback only
        if (subdir / "hnsw_index.bin").exists():
            return subdir / "hnsw_index.bin"
    return fallback


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


class HealthCheckJobResponse(BaseModel):
    """Response for POST /api/activated-repos/{user_alias}/health/check (Bug #1394)."""

    job_id: str = Field(
        description="Background job ID to poll via GET /api/jobs/{job_id}"
    )
    message: str = Field(description="Human-readable submission confirmation")


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


def _resolve_golden_repo_alias_for_activated_repo(
    activated_manager, username: str, user_alias: str
) -> Optional[str]:
    """Resolve the underlying golden repo alias for an activated repo.

    GitHub Issue #1459 AC4: the resolver-aware temporal-status helper needs
    the golden repo's BARE alias (never the activated repo's own
    user_alias) to build the sister-location pointer namespace. Reuses the
    `golden_repo_alias` field ActivatedRepoManager.get_repository() already
    tracks.

    Returns None if the activated repo cannot be found or has no tracked
    golden_repo_alias (e.g. a composite repo with no single backing golden
    repo) -- callers must treat None as "cannot resolve sister-location
    temporal data for this repo", not raise.
    """
    try:
        metadata = activated_manager.get_repository(username, user_alias, touch=False)
    except Exception as exc:
        logger.warning(
            "Failed to resolve golden_repo_alias for activated repo "
            "'%s' (user '%s'): %s",
            user_alias,
            username,
            exc,
        )
        return None
    if not metadata:
        return None
    golden_alias = metadata.get("golden_repo_alias")
    return golden_alias if isinstance(golden_alias, str) and golden_alias else None


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
    owner: Optional[str] = Query(
        None, description="Repository owner username (admin only)"
    ),
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

        # Semantic index: dynamic detection across all embedding providers
        semantic_path = resolve_semantic_index_path(index_dir)
        indexes.append(_check_index_status(semantic_path, "semantic"))

        # FTS index (tantivy) lives at .code-indexer/tantivy_index, not inside index/
        fts_path = index_dir.parent / "tantivy_index"
        indexes.append(_check_index_status(fts_path, "fts"))

        # Temporal index (any provider-aware or legacy temporal collection).
        # Local-clone scan preserved as the baseline (regression safety);
        # then resolver-aware detection (GitHub Issue #1459 AC4) overrides
        # with the resolved sister-location hnsw_index.bin when temporal
        # data has relocated per Story #1457 -- routes through the SAME
        # TemporalShardResolver/catalog mechanism the query path uses,
        # never a parallel sister-root scan.
        from code_indexer.services.temporal.temporal_collection_naming import (
            LEGACY_TEMPORAL_COLLECTION,
            is_temporal_collection as _is_temporal,
        )
        from code_indexer.services.temporal.temporal_status import (
            get_temporal_repo_status,
        )

        temporal_hnsw = next(
            (
                d / "hnsw_index.bin"
                for d in sorted(index_dir.iterdir() if index_dir.exists() else [])
                if d.is_dir()
                and _is_temporal(d.name)
                and (d / "hnsw_index.bin").exists()
            ),
            index_dir / LEGACY_TEMPORAL_COLLECTION / "hnsw_index.bin",
        )

        golden_repo_alias_for_temporal = _resolve_golden_repo_alias_for_activated_repo(
            activated_manager, target_username, user_alias
        )
        if golden_repo_alias_for_temporal:
            golden_repos_dir = (
                Path(activated_manager.activated_repos_dir).parent / "golden-repos"
            )
            temporal_status = get_temporal_repo_status(
                golden_repos_dir=golden_repos_dir,
                repo_alias=golden_repo_alias_for_temporal,
                legacy_index_path=index_dir,
            )
            if temporal_status.is_queryable and temporal_status.resolved_path:
                temporal_hnsw = temporal_status.resolved_path / "hnsw_index.bin"

        indexes.append(_check_index_status(temporal_hnsw, "temporal"))

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
    # Story #1457 AC12 remaining gap (2026-07-24 re-review, Codex round 4):
    # the None-default case already excludes "temporal" (below), but an
    # EXPLICITLY-provided index_types list must be validated against an
    # explicit allowlist -- a bare `"temporal" in request.index_types`
    # case-sensitive membership check was trivially bypassed by a case
    # variant ("Temporal") and silently accepted any other typo'd/garbage
    # value. Reject loudly on ANY unsupported entry, BEFORE any manager
    # call or job submission -- mirrors add_index_type's existing
    # route-level validation intent, generalized to a full list.
    #
    # 2026-07-24 round-5 re-review (Codex): a validate-one/use-another bug
    # -- entries were previously lowercased ONLY for the allowlist check,
    # while the raw unnormalized request.index_types was reused below for
    # the actual job submission and response body. Build ONE normalized
    # (lowercased) list HERE and use that SAME list everywhere downstream
    # -- never the raw request value again after this point. An empty
    # list ([]) is also rejected -- it would otherwise submit a
    # meaningless job with nothing to index.
    normalized_index_types: Optional[List[str]] = None
    if request.index_types is not None:
        normalized_index_types = [t.lower() for t in request.index_types]
        _valid_reindex_types = {"semantic", "fts", "scip"}
        if not normalized_index_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "index_types must not be empty. Must contain one or "
                    f"more of: {', '.join(sorted(_valid_reindex_types))}."
                ),
            )
        _invalid_types = [
            t for t in normalized_index_types if t not in _valid_reindex_types
        ]
        if _invalid_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid index type(s) {_invalid_types}. Must be one "
                    f"of: {', '.join(sorted(_valid_reindex_types))}. "
                    "Temporal indexing is never supported for activated "
                    "repositories -- temporal data is owned exclusively by "
                    "the golden repo's shared sister location."
                ),
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

        # Determine which index types to reindex. Story #1457 round-5
        # re-review (Codex): use the NORMALIZED list built and validated
        # above -- never the raw request.index_types again.
        index_types = normalized_index_types
        if index_types is None:
            # Default to all existing indexes. Story #1457 AC12
            # (2026-07-23 code review HIGH #10): "temporal" is EXCLUDED
            # here -- temporal data is owned exclusively by the golden
            # repo's shared sister location and is never built locally
            # for an activated repo (activated_repo_index_manager.py's
            # trigger_reindex rejects it unconditionally); defaulting to
            # include it here contradicted that manager-level rejection.
            index_types = ["semantic", "fts", "scip"]

        # Bug #1472: the submitted job used to be a literal no-op closure
        # (`pass`) -- the endpoint returned a genuine job_id and HTTP 202
        # while silently doing nothing. Wire the job to the REAL indexing
        # pipeline instead of inventing a new one: ActivatedRepoIndexManager
        # (server/services/activated_repo_index_manager.py) is the existing,
        # already-tested entry point that spawns real `cidx index` / `cidx
        # index --fts` / `cidx scip generate` subprocesses per index type
        # (see TestJobExecution in test_activated_repo_index_manager.py) and
        # is already reused this same way by the sibling
        # /api/v1/repos/{alias}/reindex endpoint in routers/indexing.py.
        # The already-normalized/validated `index_types` built above is
        # threaded straight through -- never re-derived.
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        index_manager_service = ActivatedRepoIndexManager(
            background_job_manager=job_manager,
            activated_repo_manager=activated_manager,
        )
        job_id = index_manager_service.trigger_reindex(
            repo_alias=user_alias,
            index_types=index_types,
            clear=False,
            username=current_user.username,
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
    # Validate index type. Story #1457 AC12 (2026-07-23 code review
    # HIGH #10): "temporal" is EXCLUDED -- temporal data is owned
    # exclusively by the golden repo's shared sister location and is
    # never built locally for an activated repo.
    valid_index_types = ["semantic", "fts", "scip"]
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

        # Bug #1473: the submitted job used to be a literal no-op closure
        # (`pass`) -- the endpoint returned a genuine job_id and HTTP 202
        # while silently doing nothing. Same defect class as #1472's
        # trigger_reindex fix: wire to the REAL existing indexing pipeline
        # (ActivatedRepoIndexManager, already reused above by trigger_reindex
        # and by the sibling /api/v1/repos/{alias}/reindex endpoint) instead
        # of inventing a new mechanism. "Adding" a single index type is
        # semantically a reindex scoped to that one type.
        from code_indexer.server.services.activated_repo_index_manager import (
            ActivatedRepoIndexManager,
        )

        index_manager_service = ActivatedRepoIndexManager(
            background_job_manager=job_manager,
            activated_repo_manager=activated_manager,
        )
        job_id = index_manager_service.trigger_reindex(
            repo_alias=user_alias,
            index_types=[index_type],
            clear=False,
            username=current_user.username,
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


@router.post(
    "/{user_alias}/health/check",
    response_model=HealthCheckJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Health check job started"},
        404: {"description": "Activated repository not found"},
        409: {
            "description": "A health check job is already running for this repository"
        },
        500: {"description": "Failed to start health check job"},
    },
)
async def check_activated_repo_health_async(
    user_alias: str,
    force_refresh: bool = Query(
        default=False, description="Bypass cache and perform fresh check"
    ),
    current_user: User = Depends(get_current_user_hybrid),
) -> HealthCheckJobResponse:
    """
    Submit a background job to check HNSW index health for an activated repo.

    Bug #1394: unlike GET /{user_alias}/health (which runs synchronously and
    can exceed the reverse-proxy timeout on repositories with dozens of
    temporal shards), this endpoint submits a background job and returns
    immediately with a job_id to poll via GET /api/jobs/{job_id}. The job's
    result is the canonical RepositoryHealthResult shape (NOT the legacy
    HealthCheckResponse wrapper the synchronous GET endpoint returns), since
    nothing currently depends on the job's result shape and this keeps
    renderHealthIndicator/renderHealthDetails on the frontend working
    unmodified.

    Args:
        user_alias: User's alias for the activated repository
        force_refresh: If True, bypass cache and perform fresh check
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        HealthCheckJobResponse with job_id to poll

    Raises:
        HTTPException 404: Repository not found
        HTTPException 409: A health check job is already running for this repo
        HTTPException 500: Failed to start health check job
    """
    try:
        activated_manager = _get_activated_repo_manager()
        repo_path = activated_manager.get_activated_repo_path(
            current_user.username, user_alias
        )

        repo_path_obj = Path(repo_path)
        if not repo_path_obj.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Activated repository '{user_alias}' not found",
            )

        index_dir = repo_path_obj / ".code-indexer" / "index"
        background_job_manager = _get_background_job_manager()

        def health_check_job() -> dict:
            result = compute_repository_health(
                user_alias,
                index_dir,
                get_shared_health_service(),
                force_refresh=force_refresh,
            )
            return result.model_dump()  # type: ignore[no-any-return]

        job_id = background_job_manager.submit_job(
            "activated_repo_health_check",
            health_check_job,
            submitter_username=current_user.username,
            repo_alias=user_alias,
        )

        return HealthCheckJobResponse(
            job_id=job_id,
            message="Health check job started",
        )

    except HTTPException:
        raise
    except DuplicateJobError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Failed to start health check job for {user_alias}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start health check job: {str(e)}",
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

        # Bug #1473: the submitted job used to be a literal no-op closure
        # (`pass`) -- the endpoint returned a genuine job_id and HTTP 202
        # while silently doing nothing. Same defect class as #1472's
        # trigger_reindex fix: wire to the REAL existing entry point --
        # ActivatedRepoManager.sync_with_golden_repository -- the same
        # method the sibling synchronous PUT /api/repos/{user_alias}/sync
        # route (routers/inline_repos.py) already calls directly, rather
        # than inventing a new sync mechanism. When the caller requests
        # reindex=True, chain a REAL follow-up reindex through the same
        # ActivatedRepoIndexManager.trigger_reindex entry point used by
        # trigger_reindex/add_index_type above -- this submits its OWN
        # job-tracked background job (dashboard/admin UI visibility per
        # CLAUDE.md's background-jobs mandate) rather than doing the
        # indexing work inline/untracked.
        def sync_job():
            """Background job to sync repository with its golden repository."""
            sync_result = activated_manager.sync_with_golden_repository(
                username=current_user.username,
                user_alias=user_alias,
            )
            if request.reindex:
                from code_indexer.server.services.activated_repo_index_manager import (
                    ActivatedRepoIndexManager,
                )

                index_manager_service = ActivatedRepoIndexManager(
                    background_job_manager=job_manager,
                    activated_repo_manager=activated_manager,
                )
                sync_result["reindex_job_id"] = index_manager_service.trigger_reindex(
                    repo_alias=user_alias,
                    index_types=["semantic", "fts", "scip"],
                    clear=False,
                    username=current_user.username,
                )
            return sync_result

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
    # AC4 (Story #981): global aliases represent shared golden repos; branch-switching
    # them is a golden-repo admin operation, not a personal-workspace operation.
    # Reject *-global aliases for all non-admin callers before touching any state.
    if user_alias.endswith("-global") and current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Cannot switch branch on global alias '{user_alias}'. "
                "Global aliases are shared golden repositories. "
                "Use change_golden_repo_branch (admin only) to change a golden repo branch, "
                "or activate a personal workspace and switch branches there."
            ),
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

        # Bug #1473: the submitted job used to be a literal no-op closure
        # (`pass`) -- the endpoint returned a genuine job_id and HTTP 202
        # while silently doing nothing. Same defect class as #1472's
        # trigger_reindex fix: wire to the REAL existing entry point --
        # ActivatedRepoManager.switch_branch -- the same method the sibling
        # synchronous PUT /api/repos/{user_alias}/branch route
        # (routers/inline_repos.py) already calls directly, rather than
        # inventing a new branch-switch mechanism. switch_branch() already
        # performs the Bug #1203 branch-aware delta reindex internally when
        # switching to a non-default branch, so this docstring's "triggers
        # a reindex" claim is satisfied by the real method itself -- no
        # separate reindex call needed here.
        def switch_branch_job():
            """Background job to switch branch for an activated repository."""
            return activated_manager.switch_branch(
                username=current_user.username,
                user_alias=user_alias,
                branch_name=request.branch_name,
                create=False,
            )

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
