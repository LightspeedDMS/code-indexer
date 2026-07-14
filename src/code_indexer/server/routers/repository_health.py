"""
Repository Health REST API Router.

Provides REST endpoints for checking HNSW index health with caching support.
"""

import logging
from pathlib import Path
from typing import Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.repository_health_aggregator import (
    CollectionHealthResult,
    RepositoryHealthResult,
    compute_repository_health,
    get_shared_health_service,
)
from code_indexer.server.services.repository_health_aggregator import (
    _to_collection_health_result as _to_collection_health_result,  # noqa: F401
)

logger = logging.getLogger(__name__)

# Bug #1394: CollectionHealthResult, RepositoryHealthResult, and
# _to_collection_health_result now live in repository_health_aggregator.py
# (shared with activated_repos.py). Re-exported here so existing imports
# from this module (e.g. tests) keep working unchanged.
__all__ = [
    "detect_semantic_index",
    "CollectionHealthResult",
    "RepositoryHealthResult",
    "IndexesStatusResponse",
    "DescriptionResponse",
    "HealthCheckJobResponse",
    "router",
]


def detect_semantic_index(index_base_path: Path) -> bool:
    """Detect whether a semantic index exists in the given index directory.

    Scans deterministically for any collection with hnsw_index.bin that is not
    a multimodal, temporal, or tantivy collection. Supports any embedding provider
    (e.g., voyage-code-3, embed-v4.0).

    Args:
        index_base_path: Path to .code-indexer/index directory.

    Returns:
        True if at least one qualifying semantic collection is found.
    """
    if not (index_base_path.exists() and index_base_path.is_dir()):
        return False
    for subdir in sorted(index_base_path.iterdir(), key=lambda p: p.name):
        if not subdir.is_dir():
            continue
        name = subdir.name
        if "multimodal" in name or "temporal" in name or "tantivy" in name:
            continue
        if (subdir / "hnsw_index.bin").exists():
            return True
    return False


class IndexesStatusResponse(BaseModel):
    """Index availability status for a repository."""

    has_semantic: bool = Field(description="Semantic index available")
    has_fts: bool = Field(description="Full-text search index available")
    has_temporal: bool = Field(description="Temporal (git history) index available")
    has_scip: bool = Field(description="SCIP code intelligence index available")


class DescriptionResponse(BaseModel):
    """cidx-meta description for a repository (Story #218)."""

    repo_alias: str = Field(description="Repository alias")
    description: str = Field(
        description="Markdown body of the cidx-meta file with frontmatter stripped"
    )


class HealthCheckJobResponse(BaseModel):
    """Response for POST /api/repositories/{repo_alias}/health/check (Bug #1394)."""

    job_id: str = Field(
        description="Background job ID to poll via GET /api/jobs/{job_id}"
    )
    message: str = Field(description="Human-readable submission confirmation")


# Create router with prefix and tags
router = APIRouter(prefix="/api/repositories", tags=["repository-health"])


def _strip_yaml_frontmatter(content: str) -> str:
    """Strip YAML frontmatter delimited by --- from markdown content.

    If the file begins with '---', everything up to and including the closing
    '---' line is removed.  The remaining body is returned.

    Args:
        content: Raw markdown file content.

    Returns:
        Markdown body with frontmatter removed, or the original content if no
        frontmatter is present.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content

    # Find the closing ---
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "".join(lines[i + 1 :])
            # Strip a single leading newline separating frontmatter from body
            if body.startswith("\n"):
                body = body[1:]
            return body

    # No closing --- found - no valid frontmatter, return original content
    return content


@router.get(
    "/{repo_alias}/description",
    response_model=DescriptionResponse,
    responses={
        200: {"description": "Description retrieved successfully"},
        404: {"description": "cidx-meta file not found for this repository"},
    },
)
async def get_repository_description(
    repo_alias: str,
    request: Request,
    current_user: User = Depends(get_current_user_hybrid),
) -> DescriptionResponse:
    """Get the cidx-meta generated description for a golden repository.

    Reads the cidx-meta markdown file for the given repository alias, strips
    YAML frontmatter, and returns the body.  Returns 404 when the file does
    not exist - there is no fallback content.

    Args:
        repo_alias: Repository alias (e.g., 'code-indexer-python')
        request: FastAPI request used to access app.state.golden_repos_dir
        current_user: Authenticated user (injected by auth dependency)

    Returns:
        DescriptionResponse with repo_alias and markdown description body

    Raises:
        HTTPException 404: cidx-meta file not found or golden_repos_dir not set
    """
    golden_repos_dir = getattr(request.app.state, "golden_repos_dir", None)
    if not golden_repos_dir:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cidx-meta description found for repository '{repo_alias}'",
        )

    # INVARIANT: cidx-meta filenames use SHORT alias ({repo_alias}.md), NOT -global.md
    cidx_meta_path = Path(golden_repos_dir) / "cidx-meta" / f"{repo_alias}.md"
    # Prevent path traversal: reject any alias that escapes the cidx-meta dir (Story #218)
    expected_parent = (Path(golden_repos_dir) / "cidx-meta").resolve()
    if not cidx_meta_path.resolve().is_relative_to(expected_parent):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cidx-meta description found for repository '{repo_alias}'",
        )
    if not cidx_meta_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cidx-meta description found for repository '{repo_alias}'",
        )

    try:
        content = cidx_meta_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(
            f"Failed to read cidx-meta file for {repo_alias}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cidx-meta description found for repository '{repo_alias}'",
        )

    description = _strip_yaml_frontmatter(content)
    return DescriptionResponse(repo_alias=repo_alias, description=description)


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


def _resolve_repository_path(repo_alias: str, current_user: User) -> Tuple[str, Path]:
    """Resolve a repo_alias to (resolved_alias, actual_repo_clone_path).

    Multi-strategy repository resolution (Story #58), shared by both the
    GET and POST health-check handlers (Bug #1394):
    1. Try as golden repo (exact match)
    2. If not found and ends with -global, try without suffix
    3. Try as user-activated repo

    Args:
        repo_alias: Repository alias as given by the caller.
        current_user: Authenticated user (for activated-repo lookup).

    Returns:
        Tuple of (resolved_alias, clone_path). resolved_alias equals
        repo_alias unless the -global suffix was stripped for a golden-repo
        match.

    Raises:
        HTTPException 404: Repository not found via any strategy.
    """
    golden_repo_manager = _get_golden_repo_manager()
    repo = golden_repo_manager.get_golden_repo(repo_alias)
    repo_path = None
    resolved_alias = repo_alias

    if not repo and repo_alias.endswith("-global"):
        base_alias = repo_alias[:-7]  # Remove "-global" suffix
        repo = golden_repo_manager.get_golden_repo(base_alias)
        if repo:
            resolved_alias = base_alias

    if not repo:
        activated_repo_manager = _get_activated_repo_manager()
        potential_path = activated_repo_manager.get_activated_repo_path(
            current_user.username, repo_alias
        )
        if Path(potential_path).exists():
            repo_path = potential_path

    if not repo and not repo_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository '{repo_alias}' not found",
        )

    if repo:
        actual_path = golden_repo_manager.get_actual_repo_path(resolved_alias)
        clone_path = Path(actual_path)
    else:
        assert repo_path is not None
        clone_path = Path(repo_path)

    return resolved_alias, clone_path


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
        _resolved_alias, clone_path = _resolve_repository_path(repo_alias, current_user)
        index_base_path = clone_path / ".code-indexer" / "index"

        return compute_repository_health(
            repo_alias,
            index_base_path,
            get_shared_health_service(),
            force_refresh=force_refresh,
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


@router.post(
    "/{repo_alias}/health/check",
    response_model=HealthCheckJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: {"description": "Health check job started"},
        404: {"description": "Repository not found"},
        409: {
            "description": "A health check job is already running for this repository"
        },
        500: {"description": "Failed to start health check job"},
    },
)
async def check_repository_health_async(
    repo_alias: str,
    force_refresh: bool = Query(
        default=False, description="Bypass cache and perform fresh check"
    ),
    current_user: User = Depends(get_current_user_hybrid),
) -> HealthCheckJobResponse:
    """
    Submit a background job to check HNSW index health for a repository.

    Bug #1394: unlike GET /{repo_alias}/health (which runs synchronously and
    can exceed the reverse-proxy timeout on repositories with dozens of
    temporal shards), this endpoint submits a background job and returns
    immediately with a job_id to poll via GET /api/jobs/{job_id}.

    Args:
        repo_alias: Repository alias (e.g., 'backend', 'frontend')
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
        resolved_alias, clone_path = _resolve_repository_path(repo_alias, current_user)
        index_base_path = clone_path / ".code-indexer" / "index"
        background_job_manager = _get_background_job_manager()

        def health_check_job() -> dict:
            result = compute_repository_health(
                resolved_alias,
                index_base_path,
                get_shared_health_service(),
                force_refresh=force_refresh,
            )
            return result.model_dump()  # type: ignore[no-any-return]

        job_id = background_job_manager.submit_job(
            "repository_health_check",
            health_check_job,
            submitter_username=current_user.username,
            repo_alias=resolved_alias,
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
        logger.error(
            f"Failed to start health check job for repository {repo_alias}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start health check job: {str(e)}",
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
    - FTS: {repo_path}/.code-indexer/tantivy_index/ directory exists
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
            assert repo_path is not None
            clone_path = Path(repo_path)

        # Check for each index type
        index_base_path = clone_path / ".code-indexer" / "index"

        # Semantic index: dynamic detection across all embedding providers
        has_semantic = detect_semantic_index(index_base_path)

        # FTS index: tantivy_index/ directory (sibling to index/, not subdirectory)
        fts_path = clone_path / ".code-indexer" / "tantivy_index"
        has_fts = fts_path.exists() and fts_path.is_dir()

        # Temporal index: temporal/ (legacy) OR any code-indexer-temporal* directory
        from code_indexer.services.temporal.temporal_collection_naming import (
            is_temporal_collection as _is_temporal,
        )

        has_temporal = False
        # Check legacy "temporal" directory first
        legacy_temporal = index_base_path / "temporal"
        if legacy_temporal.is_dir():
            has_temporal = (legacy_temporal / "hnsw_index.bin").is_file()
        # Scan for provider-aware or legacy code-indexer-temporal* directories
        if not has_temporal and index_base_path.is_dir():
            has_temporal = any(
                (d / "hnsw_index.bin").is_file()
                for d in sorted(index_base_path.iterdir())
                if d.is_dir() and _is_temporal(d.name)
            )

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
