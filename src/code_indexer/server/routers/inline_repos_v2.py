"""
Repository v2 route handlers (/api/repositories/*) extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 6 route handlers:
- GET  /api/repositories/{repo_id}
- GET  /api/repositories/{repo_id}/branches
- POST /api/repositories/{repo_id}/sync
- GET  /api/repositories/{repo_id}/stats
- GET  /api/repositories/{repo_id}/files
- POST /api/repositories/{repo_id}/search

Zero behavior change: same paths, methods, response models, and handler logic.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
)

from ..repositories.repository_listing_manager import RepositoryListingError
from ..validators.composite_repo_validator import CompositeRepoValidator
from ..services.branch_service import BranchService
from ...services.git_topology_service import GitTopologyService

from ..models.repos import (
    RepositoryDetailsV2Response,
    RepositoryStatistics,
    GitInfo,
    RepositoryConfiguration,
    RepositorySyncJobResponse,
    RepositorySyncRequest,
    SyncProgress,
    SyncJobOptions,
)
from ..models.api_models import (
    RepositoryStatsResponse,
    FileListQueryParams,
    SemanticSearchRequest,
    SemanticSearchResponse,
)
from ..models.branch_models import BranchListResponse
from ..models.activated_repository import ActivatedRepository

from ..auth import dependencies
from ..managers.composite_file_listing import _list_composite_files
from ..services.stats_service import stats_service
from ..services.file_service import file_service
from ..services.search_service import search_service
from ..app_helpers import _execute_repository_sync, _get_composite_details

# Module-level logger
logger = logging.getLogger(__name__)


def register_repos_v2_routes(
    app: FastAPI,
    *,
    activated_repo_manager,
    repository_listing_manager,
    background_job_manager,
) -> None:
    """
    Register /api/repositories/* route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures over create_app() locals before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        activated_repo_manager: ActivatedRepoManager instance
        repository_listing_manager: RepositoryListingManager instance
        background_job_manager: BackgroundJobManager instance
    """

    @app.get("/api/repositories/{repo_id}")
    def get_repository_details_v2(
        repo_id: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get detailed information about a specific repository.

        Returns comprehensive repository information including statistics,
        git info, configuration, and indexing status.

        Args:
            repo_id: Repository identifier
            current_user: Current authenticated user

        Returns:
            Detailed repository information

        Raises:
            HTTPException: 404 if repository not found, 403 if unauthorized, 400 if invalid ID
        """
        # Validate repository ID format
        if not repo_id or not repo_id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Repository ID cannot be empty",
            )

        # Clean and validate repo ID
        cleaned_repo_id = repo_id.strip()

        # Check for invalid characters and patterns
        if (
            " " in cleaned_repo_id
            or "/" in cleaned_repo_id
            or ".." in cleaned_repo_id
            or cleaned_repo_id.startswith(".")
            or len(cleaned_repo_id) > 255
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid repository ID format",
            )

        # Strategy 1: Try to find repository among user's activated repositories
        try:
            activated_repos = activated_repo_manager.list_activated_repositories(
                current_user.username
            )

            # Look for repository by user_alias (matches repo_id)
            for repo in activated_repos:
                if repo["user_alias"] == cleaned_repo_id:
                    # Check if this is a composite repository (Story 3.2)
                    if repo.get("is_composite", False):
                        # Route to composite details handler
                        try:
                            # Convert dict to ActivatedRepository model
                            from datetime import datetime as dt

                            activated_repo_model = ActivatedRepository(
                                user_alias=repo["user_alias"],
                                username=repo["username"],
                                path=Path(repo["path"]),
                                activated_at=(
                                    dt.fromisoformat(repo["activated_at"])
                                    if isinstance(repo["activated_at"], str)
                                    else repo["activated_at"]
                                ),
                                last_accessed=(
                                    dt.fromisoformat(repo["last_accessed"])
                                    if isinstance(repo["last_accessed"], str)
                                    else repo["last_accessed"]
                                ),
                                is_composite=True,
                                golden_repo_aliases=repo.get("golden_repo_aliases", []),
                                discovered_repos=repo.get("discovered_repos", []),
                            )

                            composite_details = _get_composite_details(
                                activated_repo_model
                            )
                            # Return composite details as dict for JSON response
                            return composite_details.model_dump()

                        except Exception as e:
                            raise HTTPException(
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=f"Failed to retrieve composite repository details: {str(e)}",
                            )

                    # Found activated repository (single repo) - build response from real data
                    try:
                        repo_path = activated_repo_manager.get_activated_repo_path(
                            current_user.username, cleaned_repo_id
                        )

                        # Get branch information from the activated repo
                        branch_info = activated_repo_manager.list_repository_branches(
                            current_user.username, cleaned_repo_id
                        )

                        # Get basic repository statistics
                        total_files = 0
                        total_size = 0
                        languages = set()

                        if os.path.exists(repo_path):
                            for root, dirs, files in os.walk(repo_path):
                                if ".git" in dirs:
                                    dirs.remove(".git")
                                for file in files:
                                    if not file.startswith("."):
                                        file_path = os.path.join(root, file)
                                        try:
                                            total_size += os.path.getsize(file_path)
                                            total_files += 1
                                            ext = os.path.splitext(file)[1].lower()
                                            lang_map = {
                                                ".py": "python",
                                                ".js": "javascript",
                                                ".ts": "typescript",
                                                ".java": "java",
                                                ".md": "markdown",
                                                ".yml": "yaml",
                                                ".json": "json",
                                            }
                                            if ext in lang_map:
                                                languages.add(lang_map[ext])
                                        except (OSError, IOError):
                                            continue

                        # Build response from activated repository data
                        repository_data = RepositoryDetailsV2Response(
                            id=cleaned_repo_id,
                            name=repo["golden_repo_alias"],
                            path=repo_path,
                            owner_id=current_user.username,
                            created_at=repo["activated_at"],
                            updated_at=repo["last_accessed"],
                            last_sync_at=repo["last_accessed"],
                            status="indexed",
                            indexing_progress=100.0,
                            statistics=RepositoryStatistics(
                                total_files=total_files,
                                indexed_files=total_files,
                                total_size_bytes=total_size,
                                embeddings_count=total_files * 3,
                                languages=list(languages) if languages else ["unknown"],
                            ),
                            git_info=GitInfo(
                                current_branch=branch_info.get(
                                    "current_branch", repo["current_branch"]
                                ),
                                branches=[
                                    b["name"] for b in branch_info.get("branches", [])
                                ]
                                or [repo["current_branch"]],
                                last_commit="unknown",
                                remote_url=None,
                            ),
                            configuration=RepositoryConfiguration(
                                ignore_patterns=["*.pyc", "__pycache__", ".git"],
                                chunk_size=1000,
                                overlap=200,
                                embedding_model="text-embedding-3-small",
                            ),
                            errors=[],
                        )

                        return repository_data

                    except Exception as e:
                        # If we can't get detailed info, provide basic info
                        repository_data = RepositoryDetailsV2Response(
                            id=cleaned_repo_id,
                            name=repo["golden_repo_alias"],
                            path=f"/repos/{current_user.username}/{cleaned_repo_id}",
                            owner_id=current_user.username,
                            created_at=repo["activated_at"],
                            updated_at=repo["last_accessed"],
                            last_sync_at=repo["last_accessed"],
                            status="indexed",
                            indexing_progress=100.0,
                            statistics=RepositoryStatistics(
                                total_files=0,
                                indexed_files=0,
                                total_size_bytes=0,
                                embeddings_count=0,
                                languages=["unknown"],
                            ),
                            git_info=GitInfo(
                                current_branch=repo["current_branch"],
                                branches=[repo["current_branch"]],
                                last_commit="unknown",
                                remote_url=None,
                            ),
                            configuration=RepositoryConfiguration(
                                ignore_patterns=["*.pyc", "__pycache__", ".git"],
                                chunk_size=1000,
                                overlap=200,
                                embedding_model="text-embedding-3-small",
                            ),
                            errors=[
                                f"Could not retrieve detailed information: {str(e)}"
                            ],
                        )

                        return repository_data

        except Exception:
            # Continue to try golden repositories
            pass

        # Strategy 2: Try to find repository among golden repositories
        try:
            # Check if this is a golden repository that the user can access
            golden_repo_details = repository_listing_manager.get_repository_details(
                alias=cleaned_repo_id, username=current_user.username
            )

            # Found golden repository - build response from golden repo data
            try:
                clone_path = golden_repo_details["clone_path"]
                branches = golden_repo_details.get(
                    "branches_list", [golden_repo_details["default_branch"]]
                )

                # Get basic statistics
                total_files = golden_repo_details.get("file_count", 0)
                total_size = golden_repo_details.get("index_size", 0)

                repository_data = RepositoryDetailsV2Response(
                    id=cleaned_repo_id,
                    name=golden_repo_details["alias"],
                    path=clone_path,
                    owner_id="system",
                    created_at=golden_repo_details["created_at"],
                    updated_at=golden_repo_details.get(
                        "last_updated", golden_repo_details["created_at"]
                    ),
                    last_sync_at=golden_repo_details.get("last_updated"),
                    status=(
                        "available"
                        if golden_repo_details["activation_status"] == "available"
                        else "indexed"
                    ),
                    indexing_progress=(
                        100.0
                        if golden_repo_details["activation_status"] == "activated"
                        else 0.0
                    ),
                    statistics=RepositoryStatistics(
                        total_files=total_files,
                        indexed_files=(
                            total_files
                            if golden_repo_details["activation_status"] == "activated"
                            else 0
                        ),
                        total_size_bytes=total_size,
                        embeddings_count=(
                            total_files * 3
                            if golden_repo_details["activation_status"] == "activated"
                            else 0
                        ),
                        languages=["unknown"],
                    ),
                    git_info=GitInfo(
                        current_branch=golden_repo_details["default_branch"],
                        branches=branches,
                        last_commit="unknown",
                        remote_url=golden_repo_details.get("repo_url"),
                    ),
                    configuration=RepositoryConfiguration(
                        ignore_patterns=["*.pyc", "__pycache__", ".git"],
                        chunk_size=1000,
                        overlap=200,
                        embedding_model="text-embedding-3-small",
                    ),
                    errors=[],
                )

                return repository_data

            except Exception as e:
                # Fallback with basic golden repository info
                repository_data = RepositoryDetailsV2Response(
                    id=cleaned_repo_id,
                    name=golden_repo_details["alias"],
                    path=golden_repo_details["clone_path"],
                    owner_id="system",
                    created_at=golden_repo_details["created_at"],
                    updated_at=golden_repo_details.get(
                        "last_updated", golden_repo_details["created_at"]
                    ),
                    last_sync_at=golden_repo_details.get("last_updated"),
                    status="available",
                    indexing_progress=0.0,
                    statistics=RepositoryStatistics(
                        total_files=0,
                        indexed_files=0,
                        total_size_bytes=0,
                        embeddings_count=0,
                        languages=["unknown"],
                    ),
                    git_info=GitInfo(
                        current_branch=golden_repo_details["default_branch"],
                        branches=[golden_repo_details["default_branch"]],
                        last_commit="unknown",
                        remote_url=golden_repo_details.get("repo_url"),
                    ),
                    configuration=RepositoryConfiguration(
                        ignore_patterns=["*.pyc", "__pycache__", ".git"],
                        chunk_size=1000,
                        overlap=200,
                        embedding_model="text-embedding-3-small",
                    ),
                    errors=[f"Could not retrieve detailed information: {str(e)}"],
                )

                return repository_data

        except RepositoryListingError as e:
            if "not found" in str(e):
                # Repository not found in either activated or golden repos
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Repository '{cleaned_repo_id}' not found",
                )
            else:
                # Other repository listing error
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve repository details: {str(e)}",
            )

    @app.get("/api/repositories/{repo_id}/branches", response_model=BranchListResponse)
    def list_repository_branches_v2(
        repo_id: str,
        include_remote: bool = False,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List all branches in a repository.

        Returns comprehensive branch information including current branch,
        last commit details, and index status for each branch.

        Args:
            repo_id: Repository identifier
            include_remote: Whether to include remote tracking information
            current_user: Current authenticated user

        Returns:
            List of branches with detailed information

        Raises:
            HTTPException: 404 if repository not found, 403 if unauthorized, 400 if invalid ID
        """
        # Validate repository ID format
        if not repo_id or not repo_id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Repository ID cannot be empty",
            )

        cleaned_repo_id = repo_id.strip()

        # Check for invalid characters and patterns (same validation as repository details endpoint)
        if (
            " " in cleaned_repo_id
            or "/" in cleaned_repo_id
            or ".." in cleaned_repo_id
            or cleaned_repo_id.startswith(".")
            or len(cleaned_repo_id) > 255
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid repository ID format",
            )

        try:
            # Check if repository exists and user has access (following existing pattern)
            repo_found = False
            repo_path = None

            # Look for activated repository
            if activated_repo_manager:
                activated_repos = activated_repo_manager.list_activated_repositories(
                    current_user.username
                )
                for repo in activated_repos:
                    if (
                        repo["user_alias"] == cleaned_repo_id
                        or repo["golden_repo_alias"] == cleaned_repo_id
                    ):
                        repo_found = True
                        # Construct path from activated_repos_dir + username + user_alias
                        repo_path = (
                            Path(activated_repo_manager.activated_repos_dir)
                            / current_user.username
                            / repo["user_alias"]
                        )
                        break

            if not repo_found:
                # Also check golden repositories
                try:
                    repo_details = repository_listing_manager.get_repository_details(
                        alias=cleaned_repo_id, username=current_user.username
                    )
                    repo_found = True
                    repo_path = Path(repo_details["path"])
                except RepositoryListingError:
                    pass

            if not repo_found or repo_path is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Repository '{cleaned_repo_id}' not found or not accessible",
                )

            # Validate it's not a composite repository
            CompositeRepoValidator.check_operation(repo_path, "branch_list")

            # Initialize git topology service
            git_topology_service = GitTopologyService(repo_path)

            # Use BranchService as context manager for proper resource cleanup
            with BranchService(
                git_topology_service=git_topology_service, index_status_manager=None
            ) as branch_service:
                # Get branch information
                branches = branch_service.list_branches(include_remote=include_remote)

                # Get current branch name
                current_branch_name = (
                    git_topology_service.get_current_branch() or "master"
                )

                return BranchListResponse(
                    branches=branches,
                    total=len(branches),
                    current_branch=current_branch_name,
                )

        except ValueError as e:
            # Handle git repository errors
            if "Not a git repository" in str(e):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Repository '{cleaned_repo_id}' is not a git repository",
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Git operation failed: {str(e)}",
                )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve branch information: {str(e)}",
            )

    @app.post(
        "/api/repositories/{repo_id}/sync",
        response_model=RepositorySyncJobResponse,
        status_code=202,
    )
    def sync_repository_v2(
        repo_id: str,
        sync_request: Optional[RepositorySyncRequest] = None,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Trigger manual repository synchronization with background job processing.

        Args:
            repo_id: Repository identifier to sync
            sync_request: Optional sync configuration (uses defaults if not provided)
            current_user: Current authenticated user

        Returns:
            Sync job details with tracking information

        Raises:
            HTTPException: 404 if repository not found, 409 if sync in progress, 500 for errors
        """
        # Validate repository ID format
        if not repo_id or not repo_id.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Repository ID cannot be empty",
            )

        cleaned_repo_id = repo_id.strip()

        # Use defaults if no request body provided
        if sync_request is None:
            sync_request = RepositorySyncRequest()

        try:
            # Check if repository exists and user has access
            repo_found = False

            # Look for activated repository
            if activated_repo_manager:
                activated_repos = activated_repo_manager.list_activated_repositories(
                    current_user.username
                )
                for repo in activated_repos:
                    if (
                        repo["user_alias"] == cleaned_repo_id
                        or repo["golden_repo_alias"] == cleaned_repo_id
                    ):
                        repo_found = True
                        break

            if not repo_found:
                # Also check golden repositories
                try:
                    repository_listing_manager.get_repository_details(
                        alias=cleaned_repo_id, username=current_user.username
                    )
                    repo_found = True
                except RepositoryListingError:
                    pass

            if not repo_found:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Repository '{cleaned_repo_id}' not found or not accessible",
                )

            # Check for existing sync jobs if force=False
            if not sync_request.force:
                existing_jobs = background_job_manager.get_jobs_by_operation_and_params(
                    operation_types=["sync_repository"],
                    params_filter={"repo_id": cleaned_repo_id},
                )

                # Check if any job is currently running or pending
                active_jobs = [
                    job
                    for job in existing_jobs
                    if job.get("status") in ["pending", "running"]
                    and job.get("username") == current_user.username
                ]

                if active_jobs:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Repository '{cleaned_repo_id}' sync already in progress. Use force=true to cancel existing sync.",
                    )

            # Cancel existing jobs if force=True
            if sync_request.force:
                existing_jobs = background_job_manager.get_jobs_by_operation_and_params(
                    operation_types=["sync_repository"],
                    params_filter={"repo_id": cleaned_repo_id},
                )

                for job in existing_jobs:
                    if (
                        job.get("status") in ["pending", "running"]
                        and job.get("username") == current_user.username
                    ):
                        cancel_result = background_job_manager.cancel_job(
                            job["job_id"], current_user.username
                        )
                        if cancel_result["success"]:
                            logging.info(
                                f"Cancelled existing sync job {job['job_id']} for repository {cleaned_repo_id}"
                            )

            # Submit background job for repository sync
            sync_options = {
                "incremental": sync_request.incremental,
                "force": sync_request.force,
                "full_reindex": sync_request.full_reindex,
                "pull_remote": sync_request.pull_remote,
                "remote": sync_request.remote,
                "ignore_patterns": sync_request.ignore_patterns,
                "progress_webhook": sync_request.progress_webhook,
            }

            # Create wrapper function for background job execution
            def sync_job_wrapper():
                return _execute_repository_sync(
                    repo_id=cleaned_repo_id,
                    username=current_user.username,
                    options=sync_options,
                    activated_repo_manager=activated_repo_manager,
                    progress_callback=None,
                )

            job_id = background_job_manager.submit_job(
                "sync_repository",
                sync_job_wrapper,
                submitter_username=current_user.username,
                repo_alias=cleaned_repo_id,  # AC5: Fix unknown repo bug
            )

            # Create response with job details
            created_at = datetime.now(timezone.utc)
            estimated_completion = None

            response = RepositorySyncJobResponse(
                job_id=job_id,
                status="queued",
                repository_id=cleaned_repo_id,
                created_at=created_at.isoformat(),
                estimated_completion=estimated_completion,
                progress=SyncProgress(
                    percentage=0, files_processed=0, files_total=0, current_file=None
                ),
                options=SyncJobOptions(
                    force=sync_request.force,
                    full_reindex=sync_request.full_reindex,
                    incremental=sync_request.incremental,
                ),
            )

            logging.info(
                f"Repository sync job {job_id} submitted for '{cleaned_repo_id}' by user '{current_user.username}'"
            )
            return response

        except HTTPException:
            raise
        except Exception as e:
            logging.error(f"Failed to submit repository sync job: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to submit sync job: {str(e)}",
            )

    # Repository Statistics Endpoint
    @app.get(
        "/api/repositories/{repo_id}/stats", response_model=RepositoryStatsResponse
    )
    def get_repository_stats(
        repo_id: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get comprehensive repository statistics.

        Returns detailed statistics including file counts, language distribution,
        storage metrics, activity information, and health assessment.

        Following CLAUDE.md Foundation #1: Uses real file system operations,
        no mocks or simulated data.
        """
        try:
            stats_response = stats_service.get_repository_stats(repo_id)
            return stats_response
        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{repo_id}' not found",
            )
        except PermissionError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to repository '{repo_id}'",
            )
        except Exception as e:
            logging.error(f"Failed to get repository stats for {repo_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve repository statistics: {str(e)}",
            )

    # File Listing Endpoint
    @app.get("/api/repositories/{repo_id}/files")
    def list_repository_files(
        repo_id: str,
        page: int = 1,
        limit: int = 50,
        path_pattern: Optional[str] = None,
        language: Optional[str] = None,
        sort_by: str = "path",
        path: Optional[str] = None,
        recursive: bool = False,
        content: bool = False,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List files in repository with pagination and filtering.

        Supports both single and composite repository file listing.
        For composite repos, use path and recursive parameters.
        For single repos, use existing pagination and filtering.
        If content=True and path points to a single file, return file content.
        Uses real file system operations following CLAUDE.md Foundation #1.
        """
        # If content requested and path is a file, return content
        if content and path:
            repo_dict = activated_repo_manager.get_repository(
                current_user.username, repo_id
            )
            if not repo_dict:
                raise HTTPException(status_code=404, detail="Repository not found")

            # Add missing fields required by ActivatedRepository model
            repo_dict["username"] = current_user.username
            repo_dict["path"] = activated_repo_manager.get_activated_repo_path(
                current_user.username, repo_id
            )

            repo = ActivatedRepository.from_dict(repo_dict)
            file_path = Path(repo.path) / path

            if not file_path.exists():
                raise HTTPException(status_code=404, detail=f"File '{path}' not found")

            if not file_path.is_file():
                raise HTTPException(
                    status_code=400, detail=f"Path '{path}' is not a file"
                )

            # Detect if binary
            try:
                with open(file_path, "rb") as f:
                    chunk = f.read(8192)
                    is_binary = b"\x00" in chunk
                    if not is_binary:
                        try:
                            chunk.decode("utf-8")
                        except UnicodeDecodeError:
                            is_binary = True
            except Exception:
                is_binary = True

            if is_binary:
                return {
                    "path": path,
                    "is_binary": True,
                    "size": file_path.stat().st_size,
                    "content": None,
                }
            else:
                try:
                    content_text = file_path.read_text(encoding="utf-8")
                    return {
                        "path": path,
                        "is_binary": False,
                        "size": file_path.stat().st_size,
                        "content": content_text,
                    }
                except UnicodeDecodeError:
                    return {
                        "path": path,
                        "is_binary": True,
                        "size": file_path.stat().st_size,
                        "content": None,
                    }

        # Check if this is a composite repository
        try:
            repo_dict = activated_repo_manager.get_repository(
                current_user.username, repo_id
            )
            if repo_dict and repo_dict.get("is_composite", False):
                # Composite repository - use simple file listing
                repo = ActivatedRepository.from_dict(repo_dict)
                files = _list_composite_files(
                    repo, path=path or "", recursive=recursive
                )
                return {"files": [f.model_dump() for f in files]}
        except Exception as e:
            logging.debug(f"Could not check composite status for {repo_id}: {e}")
            # Fall through to regular file listing

        # Validate query parameters
        if page < 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Page must be >= 1",
            )
        if limit < 1 or limit > 500:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Limit must be between 1 and 500",
            )

        valid_sort_fields = {"path", "size", "modified_at"}
        if sort_by not in valid_sort_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid sort field. Must be one of: {', '.join(valid_sort_fields)}",
            )

        try:
            query_params = FileListQueryParams(
                page=page,
                limit=limit,
                path_pattern=path_pattern,
                language=language,
                sort_by=sort_by,
            )

            file_list = file_service.list_files(
                repo_id, current_user.username, query_params
            )
            return file_list

        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{repo_id}' not found",
            )
        except PermissionError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to repository '{repo_id}'",
            )
        except Exception as e:
            logging.error(f"Failed to list files for repository {repo_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list repository files: {str(e)}",
            )

    # Semantic Search Endpoint
    @app.post(
        "/api/repositories/{repo_id}/search", response_model=SemanticSearchResponse
    )
    def search_repository(
        repo_id: str,
        search_request: SemanticSearchRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Perform semantic search in repository.

        Executes semantic search using real vector embeddings and Filesystem
        following CLAUDE.md Foundation #1: No mocks.
        """
        try:
            search_response = search_service.search_repository(repo_id, search_request)
            return search_response

        except FileNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Repository '{repo_id}' not found",
            )
        except PermissionError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to repository '{repo_id}'",
            )
        except Exception as e:
            logging.error(f"Failed to search repository {repo_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Search operation failed: {str(e)}",
            )
