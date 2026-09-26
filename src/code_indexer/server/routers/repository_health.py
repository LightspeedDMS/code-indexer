"""
Repository Health REST API Router.

Provides REST endpoints for checking HNSW index health with caching support.
"""

import functools
import logging
from pathlib import Path
from typing import Any, Optional, Tuple

import anyio.to_thread
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
from code_indexer.server.services.repo_access_guard import (
    AccessFilteringServiceUnavailableError,
    RepoAccessDeniedError,
    require_repo_access,
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
        HTTPException 403: caller lacks access to repo_alias
        HTTPException 404: cidx-meta file not found or golden_repos_dir not set
        HTTPException 500: access_filtering_service unavailable
    """
    # ------------------------------------------------------------------
    # Repo-level access is verified UNCONDITIONALLY, before even checking
    # golden_repos_dir, so no filesystem existence/path-traversal signal
    # is ever computed for a caller who lacks access. Offloaded off the
    # event-loop thread: this is `async def` and require_repo_access()
    # performs synchronous DB reads.
    # ------------------------------------------------------------------
    await anyio.to_thread.run_sync(
        functools.partial(
            _enforce_direct_repo_access, repo_alias, current_user.username
        )
    )

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


def _get_access_filtering_service() -> Optional[Any]:
    """Get access_filtering_service from app state.

    Mirrors the module-singleton pattern of _get_golden_repo_manager() /
    _get_activated_repo_manager() / _get_background_job_manager() above so
    it is patchable identically in tests. Unlike those siblings, returns
    None rather than raising when unwired: this is a repo-scoped access
    gate, not a hard dependency -- callers MUST turn None into a 403/500
    fail-closed response via _enforce_repo_access(), never proceed as if
    the caller had access.
    """
    from code_indexer.server import app as app_module

    return getattr(app_module.app.state, "access_filtering_service", None)


def _resolve_repo_access_target(repo_alias: str, username: str) -> Optional[str]:
    """Resolve the golden repo alias that must be authorised for repo_alias,
    in EXACTLY the same priority order check_repository_health_async() and
    get_repository_indexes() use to pick which repository's data to return
    (via _resolve_repository_path() and its inlined twin):
      1. repo_alias as a golden repo (exact match)
      2. the -global-stripped repo_alias as a golden repo
      3. the caller's OWN activated repo's backing golden alias

    Returns None when neither strategy resolves anything for this caller.

    An activated repo's custom alias is chosen by the user at activation
    time with no uniqueness check against existing golden repo aliases, so
    it can collide with an unrelated golden repo's alias. Both routes
    resolve a golden repo match BEFORE ever considering the caller's own
    activated repos for the same alias string -- this function's priority
    order must stay in exact lockstep with that, so authorisation is never
    computed against a different repository than the one actually served.
    """
    golden_repo_manager = _get_golden_repo_manager()
    if golden_repo_manager.get_golden_repo(repo_alias):
        return repo_alias
    if repo_alias.endswith("-global"):
        base_alias = repo_alias[:-7]
        if golden_repo_manager.get_golden_repo(base_alias):
            return base_alias
    return _resolve_golden_repo_alias_for_activated_repo(username, repo_alias)


def _repo_access_allowed(
    access_filtering_service: Optional[Any], repo_alias: str, username: str
) -> bool:
    """Return True if username is authorised for the SAME target
    _resolve_repo_access_target() resolves repo_alias to.

    Admin users bypass the check entirely -- checked BEFORE resolution, so
    an admin querying an alias that resolves to neither a golden nor their
    own activated repo still reaches the route's own 404, matching every
    other admin-bypass front door in this codebase.

    Raises:
        AccessFilteringServiceUnavailableError: access_filtering_service is
            None -- checked BEFORE resolution or the admin bypass, so
            callers fail closed regardless of role or what repo_alias
            would otherwise resolve to.
    """
    require_repo_access(
        access_filtering_service, username, None
    )  # raises if unavailable; else a no-op

    if access_filtering_service.is_admin_user(username):  # type: ignore[attr-defined]
        return True

    target = _resolve_repo_access_target(repo_alias, username)
    if target is None:
        return False

    try:
        require_repo_access(access_filtering_service, username, target)
        return True
    except RepoAccessDeniedError:
        return False


def _enforce_repo_access(repo_alias: str, username: str) -> None:
    """Enforce repo-level access for repo_alias against the SAME target
    the route's own resolution strategy resolves it to (see
    _resolve_repo_access_target) -- never against the raw alias string in
    isolation.

    Called UNCONDITIONALLY, before repository resolution or job
    submission (aside from the golden-repo lookups
    _resolve_repo_access_target itself performs to determine the correct
    authorisation target), so the check can never be silently skipped.

    Raises:
        HTTPException 403: caller lacks access to the resolved target.
        HTTPException 500: access_filtering_service is unavailable --
            fails closed rather than skipping the check.
    """
    try:
        allowed = _repo_access_allowed(
            _get_access_filtering_service(), repo_alias, username
        )
    except AccessFilteringServiceUnavailableError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_code": "access_control_unavailable", "detail": str(e)},
        )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "access_denied",
                "detail": str(RepoAccessDeniedError(repo_alias, username)),
            },
        )


def _enforce_direct_repo_access(repo_alias: str, username: str) -> None:
    """Enforce DIRECT repo-level access for repo_alias, with NO
    activated-repo fallback.

    GET .../description reads golden-keyed cidx-meta content
    (cidx-meta/{repo_alias}.md) unconditionally -- it never resolves
    repo_alias against the caller's own activated repos, so authorising it
    via an activated repo's backing golden alias would authorise a
    DIFFERENT repository's description than the one actually read.

    Raises:
        HTTPException 403: caller lacks direct access to repo_alias.
        HTTPException 500: access_filtering_service is unavailable --
            fails closed rather than skipping the check.
    """
    try:
        require_repo_access(_get_access_filtering_service(), username, repo_alias)
    except RepoAccessDeniedError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error_code": "access_denied", "detail": str(e)},
        )
    except AccessFilteringServiceUnavailableError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error_code": "access_control_unavailable", "detail": str(e)},
        )


def _resolve_golden_repo_alias_for_activated_repo(
    username: str, user_alias: str
) -> Optional[str]:
    """Resolve the underlying golden repo alias for an activated repo.

    GitHub Issue #1459 AC4: the resolver-aware temporal-status helper needs
    the golden repo's BARE alias (never the activated repo's own
    user_alias) to build the sister-location pointer namespace. Reuses the
    `golden_repo_alias` field ActivatedRepoManager.get_repository() already
    tracks (Story #1457's own docstring confirms this field exists,
    contrary to an earlier round's tentative "not found" report).

    Returns None if the activated repo cannot be found or has no tracked
    golden_repo_alias (e.g. a composite repo with no single backing golden
    repo) -- callers must treat None as "cannot resolve sister-location
    temporal data for this repo", not raise.
    """
    try:
        activated_repo_manager = _get_activated_repo_manager()
        metadata = activated_repo_manager.get_repository(
            username, user_alias, touch=False
        )
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
        HTTPException 403: caller lacks access to repo_alias
        HTTPException 404: Repository not found
        HTTPException 409: A health check job is already running for this repo
        HTTPException 500: Failed to start health check job, or
            access_filtering_service unavailable
    """
    # ------------------------------------------------------------------
    # Repo-level access is verified UNCONDITIONALLY, before repository
    # resolution or job submission. Offloaded off the event-loop thread:
    # this is `async def` and require_repo_access() performs synchronous
    # DB reads.
    # ------------------------------------------------------------------
    await anyio.to_thread.run_sync(
        functools.partial(_enforce_repo_access, repo_alias, current_user.username)
    )

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
        HTTPException 403: caller lacks access to repo_alias
        HTTPException 404: Repository not found
        HTTPException 500: Failed to check index status, or
            access_filtering_service unavailable
    """
    # ------------------------------------------------------------------
    # Repo-level access is verified UNCONDITIONALLY, before repository
    # resolution. Offloaded off the event-loop thread: this is `async def`
    # and require_repo_access() performs synchronous
    # DB reads.
    # ------------------------------------------------------------------
    await anyio.to_thread.run_sync(
        functools.partial(_enforce_repo_access, repo_alias, current_user.username)
    )

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

        # Temporal index: legacy bare "temporal" dir (pre-#1290, no
        # sister-location equivalent, never migrated -- kept as a pure
        # local-clone check) OR resolver-aware detection of
        # code-indexer-temporal* namespaces, which may have relocated to
        # Story #1457's golden-owned sister location (GitHub Issue #1459
        # AC4) -- routes through the SAME TemporalShardResolver/catalog
        # mechanism the query path uses, never a parallel sister-root scan.
        from code_indexer.services.temporal.temporal_status import (
            get_temporal_repo_status,
        )

        has_temporal = False
        # Check legacy "temporal" directory first
        legacy_temporal = index_base_path / "temporal"
        if legacy_temporal.is_dir():
            has_temporal = (legacy_temporal / "hnsw_index.bin").is_file()

        if not has_temporal:
            golden_repo_alias_for_temporal = (
                resolved_alias
                if repo
                else _resolve_golden_repo_alias_for_activated_repo(
                    current_user.username, repo_alias
                )
            )
            if golden_repo_alias_for_temporal:
                golden_repos_dir = (
                    Path(_get_activated_repo_manager().activated_repos_dir).parent
                    / "golden-repos"
                )
                temporal_status = get_temporal_repo_status(
                    golden_repos_dir=golden_repos_dir,
                    repo_alias=golden_repo_alias_for_temporal,
                    legacy_index_path=index_base_path,
                )
                has_temporal = temporal_status.is_queryable

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
