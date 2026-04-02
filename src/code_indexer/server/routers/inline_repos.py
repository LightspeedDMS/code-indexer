"""
Repository route handlers extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 14 route handlers:
- GET  /api/repos
- POST /api/repos/activate
- DELETE /api/repos/{user_alias}
- GET  /api/repos/activation/{job_id}/progress
- PUT  /api/repos/{user_alias}/branch
- GET  /api/repos/discover
- PUT  /api/repos/{user_alias}/sync
- POST /api/repos/sync
- GET  /api/repos/{user_alias}/branches
- GET  /api/repos/golden/{alias}
- GET  /api/repos/golden/{alias}/branches
- GET  /api/repos/available
- GET  /api/repos/status
- GET  /api/repos/{user_alias}

Zero behavior change: same paths, methods, response models, and handler logic.
Large method sizes are pre-existing in the source inline_routes.py (3221 lines).
"""

import logging
import os
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Any, Optional

from ..repositories.activated_repo_manager import ActivatedRepoError  # noqa: E402
from ..repositories.repository_listing_manager import (
    RepositoryListingError,
)  # noqa: E402
from ..services.repository_discovery_service import (
    RepositoryDiscoveryError,
)  # noqa: E402
from ..validators.composite_repo_validator import CompositeRepoValidator  # noqa: E402
from ..repositories.golden_repo_manager import GitOperationError  # noqa: E402

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Query,
)

from ..models.auth import MessageResponse
from ..models.repos import (
    ActivateRepositoryRequest,
    ActivatedRepositoryInfo,
    SwitchBranchRequest,
    RepositoryInfo,
    RepositoryDetailsResponse,
    RepositoryListResponse,
    AvailableRepositoryListResponse,
    RepositorySyncResponse,
    BranchInfo,
    RepositoryBranchesResponse,
    SyncProgress,
    SyncJobOptions,
    RepositorySyncJobResponse,
    GeneralRepositorySyncRequest,
)
from ..models.jobs import JobResponse
from ..models.api_models import (
    RepositoryStatusSummary,
    ActivatedRepositorySummary,
    AvailableRepositorySummary,
    RecentActivity,
)
from ..models.repository_discovery import RepositoryDiscoveryResponse

from ..auth import dependencies
from ..app_helpers import (
    _execute_repository_sync,
)

# Module-level logger
logger = logging.getLogger(__name__)


def register_repo_routes(
    app: FastAPI,
    *,
    activated_repo_manager,
    golden_repo_manager,
    repository_listing_manager,
    background_job_manager,
) -> None:
    """
    Register repository route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures over create_app() locals before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        activated_repo_manager: ActivatedRepoManager instance
        golden_repo_manager: GoldenRepoManager instance
        repository_listing_manager: RepositoryListingManager instance
        background_job_manager: BackgroundJobManager instance
    """

    # Protected endpoints (require authentication)
    @app.get("/api/repos", response_model=RepositoryListResponse)
    def list_repositories(
        filter: Optional[str] = None,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List activated repositories for current user.

        Args:
            filter: Optional filter pattern for repository aliases
            current_user: Current authenticated user

        Returns:
            List of activated repositories for the user
        """
        try:
            repos = activated_repo_manager.list_activated_repositories(
                current_user.username
            )

            # Apply filter if provided
            if filter:
                filtered_repos = []
                for repo in repos:
                    if filter.lower() in repo["user_alias"].lower():
                        filtered_repos.append(repo)
                repos = filtered_repos

            # Return wrapped in RepositoryListResponse for consistency
            return RepositoryListResponse(
                repositories=[ActivatedRepositoryInfo(**repo) for repo in repos],
                total=len(repos),
            )

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list repositories: {str(e)}",
            )

    @app.post("/api/repos/activate", response_model=JobResponse, status_code=202)
    def activate_repository(
        request: ActivateRepositoryRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_power_user),
    ):
        """
        Activate repository for querying (power user or admin only) - async operation.

        Supports both single repository and composite repository activation.

        Args:
            request: Repository activation request data
            current_user: Current authenticated power user or admin

        Returns:
            Job ID and message for tracking the async operation

        Raises:
            HTTPException: If golden repository not found or already activated
        """
        try:
            job_id = activated_repo_manager.activate_repository(
                username=current_user.username,
                golden_repo_alias=request.golden_repo_alias,
                golden_repo_aliases=request.golden_repo_aliases,
                branch_name=request.branch_name,
                user_alias=request.user_alias,
            )

            # Determine appropriate user_alias for response message
            if request.golden_repo_aliases:
                # Composite activation
                user_alias_str: str = request.user_alias or "composite_repository"
                repo_count = len(request.golden_repo_aliases)
                return JobResponse(
                    job_id=job_id,
                    message=f"Composite repository '{user_alias_str}' activation started for user '{current_user.username}' ({repo_count} repositories)",
                )
            else:
                # Single repository activation
                user_alias_str = (
                    request.user_alias or request.golden_repo_alias or "repository"
                )
                return JobResponse(
                    job_id=job_id,
                    message=f"Repository '{user_alias_str}' activation started for user '{current_user.username}'",
                )

        except ActivatedRepoError as e:
            error_msg = str(e)

            if "not found" in error_msg:
                # Provide repository suggestions
                available_repos = golden_repo_manager.list_golden_repos()
                suggestions = [repo.get("alias", "") for repo in available_repos[:5]]

                detail: Dict[str, Any] = {
                    "error": error_msg,
                    "available_repositories": suggestions,
                    "guidance": "Use 'GET /api/repos/golden' to see all available repositories",
                }

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=detail,
                )
            elif "already activated" in error_msg:
                # Provide conflict resolution guidance
                user_alias_conflict: str = (
                    request.user_alias or request.golden_repo_alias or "repository"
                )
                conflict_detail: Dict[str, Any] = {
                    "error": error_msg,
                    "conflict_resolution": {
                        "options": [
                            {
                                "action": "switch_branch",
                                "description": f"Switch to different branch in existing repository '{user_alias_conflict}'",
                                "endpoint": f"PUT /api/repos/{user_alias_conflict}/branch",
                            },
                            {
                                "action": "use_different_alias",
                                "description": "Choose a different user_alias for this activation",
                                "example": f"{user_alias_conflict}_v2",
                            },
                            {
                                "action": "deactivate_first",
                                "description": f"Deactivate existing repository '{user_alias_conflict}' before reactivating",
                                "endpoint": f"DELETE /api/repos/{user_alias_conflict}",
                            },
                        ]
                    },
                    "guidance": "Repository conflicts occur when trying to activate a repository with an alias that's already in use",
                }

                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=conflict_detail,
                )
            else:
                # Generic bad request with troubleshooting guidance
                troubleshoot_detail: Dict[str, Any] = {
                    "error": error_msg,
                    "troubleshooting": {
                        "common_causes": [
                            "Invalid branch name specified",
                            "Insufficient permissions",
                            "Repository corruption in golden repository",
                        ],
                        "recommended_actions": [
                            "Verify the golden repository exists: GET /api/repos/golden",
                            "Check available branches: GET /api/repos/golden/{alias}/branches",
                            "Ensure you have power user privileges",
                        ],
                    },
                }

                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=troubleshoot_detail,
                )
        except Exception as e:
            # Log the error for administrative review
            logging.error(
                f"Repository activation failed for user '{current_user.username}': {str(e)}",
                extra={
                    "username": current_user.username,
                    "golden_repo_alias": request.golden_repo_alias,
                    "branch_name": request.branch_name,
                    "user_alias": request.user_alias,
                    "error_type": type(e).__name__,
                },
            )

            detail = {
                "error": f"Internal error during repository activation: {str(e)}",
                "administrative_guidance": "This error has been logged for administrator review",
                "user_actions": [
                    "Try again in a few minutes",
                    "Contact administrator if problem persists",
                    "Check system status at /api/health",
                ],
            }

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail,
            )

    @app.delete("/api/repos/{user_alias}", response_model=JobResponse, status_code=202)
    def deactivate_repository(
        user_alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Deactivate repository for current user - async operation.

        Args:
            user_alias: User's alias for the repository to deactivate
            current_user: Current authenticated user

        Returns:
            Job ID and message for tracking the async operation

        Raises:
            HTTPException: If repository not found
        """
        try:
            job_id = activated_repo_manager.deactivate_repository(
                username=current_user.username,
                user_alias=user_alias,
            )

            return JobResponse(
                job_id=job_id,
                message=f"Repository '{user_alias}' deactivation started for user '{current_user.username}'",
            )

        except ActivatedRepoError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to deactivate repository: {str(e)}",
            )

    @app.get("/api/repos/activation/{job_id}/progress")
    def get_activation_progress(
        job_id: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get real-time activation progress for monitoring.

        Args:
            job_id: Job ID returned from activation request
            current_user: Current authenticated user

        Returns:
            Real-time progress information including step details

        Raises:
            HTTPException: If job not found or access denied
        """
        try:
            # Get job status from background job manager
            job_status = activated_repo_manager.background_job_manager.get_job_status(
                job_id, current_user.username
            )

            if not job_status:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": f"Activation job '{job_id}' not found",
                        "guidance": "Job may have expired or you may not have permission to view it",
                        "troubleshooting": [
                            "Verify the job ID is correct",
                            "Check if the job belongs to your user account",
                            "Jobs older than 24 hours may be automatically cleaned up",
                        ],
                    },
                )

            # Enhance job status with activation-specific details
            enhanced_status = {
                "job_id": job_status["job_id"],
                "status": job_status["status"],
                "progress_percentage": job_status["progress"],
                "created_at": job_status["created_at"],
                "started_at": job_status["started_at"],
                "completed_at": job_status["completed_at"],
                "operation_type": job_status["operation_type"],
                "error": job_status.get("error"),
                "result": job_status.get("result"),
            }

            # Add progress interpretation for activation jobs
            if job_status["operation_type"] == "activate_repository":
                progress = job_status["progress"]
                if progress == 0 and job_status["status"] == "pending":
                    enhanced_status["current_step"] = "Queued for processing"
                    enhanced_status["next_step"] = "Validation and setup"
                elif progress <= 20:
                    enhanced_status["current_step"] = "Validating golden repository"
                    enhanced_status["next_step"] = "Creating user directory structure"
                elif progress <= 40:
                    enhanced_status["current_step"] = "Setting up workspace"
                    enhanced_status["next_step"] = "Cloning repository"
                elif progress <= 60:
                    enhanced_status["current_step"] = "Cloning repository data"
                    enhanced_status["next_step"] = "Configuring branches"
                elif progress <= 80:
                    enhanced_status["current_step"] = "Configuring repository branches"
                    enhanced_status["next_step"] = "Creating metadata"
                elif progress <= 95:
                    enhanced_status["current_step"] = "Finalizing setup"
                    enhanced_status["next_step"] = "Completing activation"
                elif progress == 100:
                    enhanced_status["current_step"] = (
                        "Activation completed successfully"
                    )
                    enhanced_status["next_step"] = "Repository ready for use"
                else:
                    enhanced_status["current_step"] = "Processing"
                    enhanced_status["next_step"] = "Please wait"

                # Add time estimation
                if job_status["started_at"] and job_status["status"] == "running":
                    from datetime import datetime, timezone

                    started = datetime.fromisoformat(
                        job_status["started_at"].replace("Z", "+00:00")
                    )
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds()

                    # Estimate total time based on progress (typical activation: 30-120 seconds)
                    if progress > 0:
                        estimated_total = (elapsed / progress) * 100
                        estimated_remaining = max(0, estimated_total - elapsed)
                        enhanced_status["time_estimates"] = {
                            "elapsed_seconds": round(elapsed),
                            "estimated_remaining_seconds": round(estimated_remaining),
                            "estimated_total_seconds": round(estimated_total),
                        }

            return enhanced_status

        except HTTPException:
            raise
        except Exception as e:
            logging.error(
                f"Failed to get activation progress for job '{job_id}': {str(e)}",
                extra={
                    "job_id": job_id,
                    "username": current_user.username,
                    "error_type": type(e).__name__,
                },
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": f"Failed to retrieve activation progress: {str(e)}",
                    "guidance": "This error has been logged for administrator review",
                },
            )

    @app.put("/api/repos/{user_alias}/branch", response_model=MessageResponse)
    def switch_repository_branch(
        user_alias: str,
        request: SwitchBranchRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Switch branch for an activated repository.

        Args:
            user_alias: User's alias for the repository
            request: Branch switching request data
            current_user: Current authenticated user

        Returns:
            Success message

        Raises:
            HTTPException: If repository not found or branch switch fails
        """
        try:
            # Get repository path and validate it's not a composite repository
            repo_path = activated_repo_manager.get_activated_repo_path(
                username=current_user.username,
                user_alias=user_alias,
            )
            CompositeRepoValidator.check_operation(Path(repo_path), "branch_switch")

            result = activated_repo_manager.switch_branch(
                username=current_user.username,
                user_alias=user_alias,
                branch_name=request.branch_name,
                create=request.create,
            )

            return MessageResponse(message=result["message"])

        except ActivatedRepoError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        except GitOperationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to switch branch: {str(e)}",
            )

    # Repository Discovery Endpoint
    @app.get("/api/repos/discover", response_model=RepositoryDiscoveryResponse)
    def discover_repositories(
        source: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Discover matching repositories by git origin URL or source pattern.

        Finds matching golden and activated repositories based on the provided
        source pattern, returning repository candidates for intelligent client linking.

        Args:
            source: Git repository URL or source pattern to search for
            current_user: Current authenticated user

        Returns:
            Repository discovery response with matching repositories

        Raises:
            HTTPException: 400 if invalid URL, 401 if unauthorized, 500 if server error
        """
        try:
            # Initialize repository discovery service
            from .services.repository_discovery_service import (
                RepositoryDiscoveryService,
            )

            discovery_service = RepositoryDiscoveryService(
                golden_repo_manager=golden_repo_manager,
                activated_repo_manager=activated_repo_manager,
            )

            # Discover matching repositories
            discovery_response = discovery_service.discover_repositories(
                repo_url=source,
                user=current_user,
            )

            logging.info(
                f"Repository discovery for {source} by {current_user.username}: "
                f"{discovery_response.total_matches} matches found"
            )

            return discovery_response

        except RepositoryDiscoveryError as e:
            # Handle known discovery errors with appropriate status codes
            if "Invalid git URL" in str(e):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid git URL format: {str(e)}",
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Repository discovery failed: {str(e)}",
                )

        except Exception as e:
            logging.error(f"Unexpected error in repository discovery: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Repository discovery operation failed: {str(e)}",
            )

    # NOTE: Moved generic {user_alias} route after specific routes to avoid path conflicts

    @app.put("/api/repos/{user_alias}/sync", response_model=RepositorySyncResponse)
    def sync_repository(
        user_alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Sync activated repository with its golden repository.

        Fetches latest changes from the golden repository and merges them
        into the activated repository's current branch.

        Args:
            user_alias: User's alias for the repository
            current_user: Current authenticated user

        Returns:
            Sync operation result with details about changes applied

        Raises:
            HTTPException: If repository not found or sync operation fails
        """
        try:
            # Get repository path and validate it's not a composite repository
            repo_path = activated_repo_manager.get_activated_repo_path(
                username=current_user.username,
                user_alias=user_alias,
            )
            CompositeRepoValidator.check_operation(Path(repo_path), "sync")

            result = activated_repo_manager.sync_with_golden_repository(
                username=current_user.username,
                user_alias=user_alias,
            )

            return RepositorySyncResponse(
                message=result["message"],
                changes_applied=result["changes_applied"],
                files_changed=result.get("files_changed"),
                changed_files=result.get("changed_files"),
            )

        except ActivatedRepoError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        except GitOperationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to sync repository: {str(e)}",
            )

    @app.post(
        "/api/repos/sync", response_model=RepositorySyncJobResponse, status_code=202
    )
    def sync_repository_general(
        sync_request: GeneralRepositorySyncRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Trigger manual repository synchronization with repository alias in request body.

        This endpoint provides a general sync API that accepts the repository alias
        in the request body instead of the URL path, matching the format expected
        by manual testing and external API consumers.

        This endpoint supports the same functionality as POST /api/repositories/{repo_id}/sync
        but with a more convenient request format for general usage.

        Args:
            sync_request: Repository sync configuration including repository alias
            current_user: Current authenticated user

        Returns:
            Sync job details with tracking information

        Raises:
            HTTPException: 404 if repository not found, 409 if sync in progress, 500 for errors
        """
        try:
            # Extract repository alias from request body
            input_alias = sync_request.repository_alias

            # Clean and validate repository alias
            cleaned_input_alias = input_alias.strip()
            if not cleaned_input_alias:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Repository alias cannot be empty",
                )

            def resolve_repository_alias_to_id(alias: str, username: str) -> str:
                """
                Resolve repository alias to actual repository ID.

                Args:
                    alias: Input alias from user request
                    username: Current user's username

                Returns:
                    Resolved repository ID

                Raises:
                    HTTPException: If alias cannot be resolved or access denied
                """
                try:
                    activated_repos = (
                        activated_repo_manager.list_activated_repositories(username)
                    )

                    # Strategy 1: Look for exact user_alias match
                    for repo in activated_repos:
                        if repo["user_alias"] == alias:
                            # Return the actual repository ID if available, otherwise use user_alias
                            return str(repo.get("actual_repo_id", repo["user_alias"]))

                    # Strategy 2: Look for golden_repo_alias match
                    for repo in activated_repos:
                        if repo.get("golden_repo_alias") == alias:
                            return str(repo.get("actual_repo_id", repo["user_alias"]))

                    # Strategy 3: Check if alias is already a repository ID
                    for repo in activated_repos:
                        if repo.get("actual_repo_id") == alias:
                            return alias  # Already resolved ID

                    # Strategy 4: Fall back to repository listing manager for discovery
                    try:
                        repository_listing_manager.get_repository_details(
                            alias, username
                        )
                        # If this succeeds, the alias exists but might not be activated
                        # Return the alias as the ID for now
                        return alias
                    except Exception:
                        pass

                    # If all strategies fail, raise not found error
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=(
                            f"Repository alias '{alias}' could not be resolved to a valid "
                            f"repository ID for user '{username}'"
                        ),
                    )

                except HTTPException:
                    raise  # Re-raise HTTP exceptions
                except Exception as e:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Error resolving repository alias '{alias}': {str(e)}",
                    )

            # Resolve the alias to actual repository ID
            resolved_repo_id = resolve_repository_alias_to_id(
                cleaned_input_alias, current_user.username
            )

            # Use resolved repository ID for all subsequent operations
            cleaned_repo_id = resolved_repo_id

            # Check for existing sync jobs if force=False
            if not sync_request.force:
                existing_jobs = background_job_manager.get_jobs_by_operation_and_params(
                    operation_types=["sync_repository"],
                    params_filter={"repo_id": cleaned_repo_id},
                )

                if existing_jobs:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Repository '{cleaned_repo_id}' sync already in progress. Use force=true to cancel existing sync.",
                    )

            # Cancel existing sync jobs if force=True
            if sync_request.force:
                existing_jobs = background_job_manager.get_jobs_by_operation_and_params(
                    operation_types=["sync_repository"],
                    params_filter={"repo_id": cleaned_repo_id},
                )

                critical_cancellation_failures = []
                minor_cancellation_failures = []

                for job in existing_jobs:
                    try:
                        background_job_manager.cancel_job(
                            job["job_id"], current_user.username
                        )
                        logging.info(
                            f"Cancelled existing sync job {job['job_id']} for repository {cleaned_repo_id}"
                        )
                    except Exception as e:
                        error_message = str(e).lower()
                        job_id = job["job_id"]

                        # Categorize failure types
                        if any(
                            critical_keyword in error_message
                            for critical_keyword in [
                                "locked",
                                "permission denied",
                                "access denied",
                                "critical",
                                "in progress",
                            ]
                        ):
                            critical_cancellation_failures.append(
                                {"job_id": job_id, "error": str(e), "type": "critical"}
                            )
                            logging.error(
                                f"Critical cancellation failure for job {job_id}: {str(e)}"
                            )
                        else:
                            minor_cancellation_failures.append(
                                {"job_id": job_id, "error": str(e), "type": "minor"}
                            )
                            logging.warning(
                                f"Minor cancellation failure for job {job_id}: {str(e)}"
                            )

                # If there are critical cancellation failures, abort the new sync
                if critical_cancellation_failures:
                    failed_jobs = [
                        f"job {f['job_id']}: {f['error']}"
                        for f in critical_cancellation_failures
                    ]
                    error_details = "; ".join(failed_jobs)
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Cannot proceed with sync - critical cancellation failures: "
                            f"{error_details}. Some existing jobs could not be safely cancelled."
                        ),
                    )

                # Log minor failures but proceed with sync
                if minor_cancellation_failures:
                    failed_jobs = [
                        f"job {f['job_id']}" for f in minor_cancellation_failures
                    ]
                    logging.info(
                        f"Proceeding with sync despite minor cancellation failures: "
                        f"{', '.join(failed_jobs)}"
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

            def create_webhook_callback(
                webhook_url: Optional[str],
            ) -> Optional[Callable[[int], None]]:
                """Create a webhook callback function if webhook URL is provided."""
                if not webhook_url:
                    return None

                def webhook_callback(progress: int) -> None:
                    """Send progress updates to webhook URL."""
                    try:
                        payload = {
                            "repository_id": cleaned_repo_id,
                            "progress": progress,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "username": current_user.username,
                        }
                        response = requests.post(
                            webhook_url,
                            json=payload,
                            timeout=5,  # Don't wait too long for webhook responses
                            headers={"Content-Type": "application/json"},
                        )
                        # Log webhook failures but don't interrupt sync
                        if not response.ok:
                            logging.warning(
                                f"Webhook {webhook_url} returned {response.status_code}"
                            )
                    except Exception as e:
                        logging.warning(
                            f"Failed to send webhook to {webhook_url}: {str(e)}"
                        )

                return webhook_callback

            def sync_job_wrapper():
                # Create webhook callback if webhook URL provided
                webhook_url: Optional[str] = sync_options.get("progress_webhook")  # type: ignore[assignment]
                webhook_callback = create_webhook_callback(webhook_url)

                return _execute_repository_sync(
                    repo_id=cleaned_repo_id,
                    username=current_user.username,
                    options=sync_options,
                    activated_repo_manager=activated_repo_manager,
                    progress_callback=webhook_callback,
                )

            job_id = background_job_manager.submit_job(
                "sync_repository",
                sync_job_wrapper,
                submitter_username=current_user.username,
                repo_alias=cleaned_repo_id,  # AC5: Fix unknown repo bug
            )

            # Return job details
            return RepositorySyncJobResponse(
                job_id=job_id,
                status="queued",
                repository_id=cleaned_repo_id,
                created_at=datetime.now(timezone.utc).isoformat(),
                estimated_completion=None,
                progress=SyncProgress(
                    percentage=0, files_processed=0, files_total=0, current_file=None
                ),
                options=SyncJobOptions(
                    force=sync_request.force,
                    full_reindex=sync_request.full_reindex,
                    incremental=sync_request.incremental,
                ),
            )

        except HTTPException:
            # Re-raise HTTPExceptions as-is
            raise
        except Exception as e:
            logging.error(f"Failed to submit general repository sync job: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to submit sync job: {str(e)}",
            )

    @app.get(
        "/api/repos/{user_alias}/branches", response_model=RepositoryBranchesResponse
    )
    def list_repository_branches(
        user_alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List all branches in an activated repository.

        Returns both local and remote branches with detailed information
        including commit details and current branch indicator.

        Args:
            user_alias: User's alias for the repository
            current_user: Current authenticated user

        Returns:
            Branch listing with detailed information

        Raises:
            HTTPException: If repository not found or branch listing fails
        """
        try:
            result = activated_repo_manager.list_repository_branches(
                username=current_user.username,
                user_alias=user_alias,
            )

            # Convert the result to the response model
            branches = [
                BranchInfo(
                    name=branch["name"],
                    type=branch["type"],
                    is_current=branch["is_current"],
                    remote_ref=branch.get("remote_ref"),
                    last_commit_hash=branch.get("last_commit_hash"),
                    last_commit_message=branch.get("last_commit_message"),
                    last_commit_date=branch.get("last_commit_date"),
                )
                for branch in result["branches"]
            ]

            return RepositoryBranchesResponse(
                branches=branches,
                current_branch=result["current_branch"],
                total_branches=result["total_branches"],
                local_branches=result["local_branches"],
                remote_branches=result["remote_branches"],
            )

        except ActivatedRepoError as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),
            )
        except GitOperationError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list repository branches: {str(e)}",
            )

    # NOTE: Routes moved before generic {user_alias} route to avoid path conflicts

    # NOTE: Routes moved before generic {user_alias} route to avoid path conflicts

    @app.get("/api/repos/golden/{alias}", response_model=RepositoryDetailsResponse)
    def get_golden_repository_details(
        alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get detailed information about a specific golden repository.

        Args:
            alias: Repository alias to get details for
            current_user: Current authenticated user

        Returns:
            Detailed repository information including activation status

        Raises:
            HTTPException: If repository not found
        """
        try:
            details = repository_listing_manager.get_repository_details(
                alias=alias, username=current_user.username
            )

            return RepositoryDetailsResponse(**details)

        except RepositoryListingError as e:
            if "not found" in str(e):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=str(e),
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(e),
                )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get repository details: {str(e)}",
            )

    @app.get("/api/repos/golden/{alias}/branches")
    def list_golden_repository_branches(
        alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List all branches for a golden repository.

        Args:
            alias: Repository alias to list branches for
            current_user: Current authenticated user

        Returns:
            GoldenRepositoryBranchesResponse with branch information

        Raises:
            HTTPException: 404 if repository not found, 403 if access denied, 500 for errors
        """
        try:
            # Check if golden repository exists
            if not golden_repo_manager.golden_repo_exists(alias):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Golden repository '{alias}' not found",
                )

            # Check user permissions
            if not golden_repo_manager.user_can_access_golden_repo(alias, current_user):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: Cannot access golden repository '{alias}'",
                )

            # Get branch information
            branches = golden_repo_manager.get_golden_repo_branches(alias)

            # Find default branch
            default_branch = None
            for branch in branches:
                if branch.is_default:
                    default_branch = branch.name
                    break

            # Create response
            from code_indexer.server.models.golden_repo_branch_models import (
                GoldenRepositoryBranchesResponse,
            )

            response = GoldenRepositoryBranchesResponse(
                repository_alias=alias,
                total_branches=len(branches),
                default_branch=default_branch,
                branches=branches,
                retrieved_at=datetime.now(timezone.utc),
            )

            return response

        except HTTPException:
            # Re-raise HTTP exceptions as-is
            raise
        except GitOperationError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Git operation failed: {str(e)}",
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list repository branches: {str(e)}",
            )

    # Repository Available Endpoint - must be defined BEFORE generic {user_alias} route
    @app.get("/api/repos/available", response_model=AvailableRepositoryListResponse)
    def list_available_repositories(
        search: Optional[str] = None,
        repo_status: Optional[str] = None,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List available golden repositories for current user.

        Args:
            search: Optional search term to filter repositories
            repo_status: Optional status filter ("available" or "activated")
            current_user: Current authenticated user

        Returns:
            List of available repositories

        Raises:
            HTTPException: If query parameters are invalid
        """
        try:
            result = repository_listing_manager.list_available_repositories(
                username=current_user.username,
                search_term=search,
                status_filter=repo_status,
            )

            # Apply access filtering based on user's group membership (Story #707 AC4)
            filtered_repos = result["repositories"]
            if (
                hasattr(app.state, "access_filtering_service")
                and app.state.access_filtering_service
            ):
                repo_aliases = [repo["alias"] for repo in filtered_repos]
                accessible_aliases = (
                    app.state.access_filtering_service.filter_repo_listing(
                        repo_aliases, current_user.username
                    )
                )
                filtered_repos = [
                    repo
                    for repo in filtered_repos
                    if repo["alias"] in accessible_aliases
                ]

            # Convert to response model
            repositories = [
                RepositoryInfo(
                    alias=repo["alias"],
                    repo_url=repo["repo_url"],
                    default_branch=repo["default_branch"],
                    created_at=repo["created_at"],
                )
                for repo in filtered_repos
            ]

            return AvailableRepositoryListResponse(
                repositories=repositories,
                total=len(repositories),
            )

        except RepositoryListingError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(e),
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list repositories: {str(e)}",
            )

    # Repository Status Endpoint - must be defined BEFORE generic {user_alias} route
    @app.get("/api/repos/status", response_model=RepositoryStatusSummary)
    def get_repository_status_summary(
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get comprehensive repository status summary.

        Provides an overview of:
        - Activated repositories with sync status
        - Available repositories for activation
        - Recent activity and recommendations
        """
        try:
            # Get activated repository manager
            activated_manager = activated_repo_manager

            # Get golden repository manager
            golden_manager = golden_repo_manager

            # Get activated repositories for current user
            activated_repos = activated_manager.list_activated_repositories(
                current_user.username
            )

            # Calculate activated repository statistics
            total_activated = len(activated_repos)
            synced_count = 0
            needs_sync_count = 0
            conflict_count = 0
            recent_activations = []

            for repo in activated_repos:
                if repo.get("sync_status") == "synced":
                    synced_count += 1
                elif repo.get("sync_status") == "needs_sync":
                    needs_sync_count += 1
                elif repo.get("sync_status") == "conflict":
                    conflict_count += 1

                # Add to recent activations if activated within last 7 days
                from datetime import datetime, timezone, timedelta

                try:
                    activation_date_str = repo.get("activated_at")
                    if activation_date_str:
                        activation_date = datetime.fromisoformat(
                            activation_date_str.replace("Z", "+00:00")
                        )
                        if activation_date > datetime.now(timezone.utc) - timedelta(
                            days=7
                        ):
                            recent_activations.append(
                                {
                                    "alias": repo.get("user_alias"),
                                    "activation_date": activation_date_str,
                                }
                            )
                except (ValueError, AttributeError):
                    pass

            # Get available repositories (golden repositories)
            available_repos = golden_manager.list_golden_repos()
            total_available = len(available_repos)

            # Count not activated repositories
            activated_aliases = {
                repo.get("user_alias")
                for repo in activated_repos
                if repo.get("user_alias")
            }
            not_activated_count = sum(
                1
                for repo in available_repos
                if repo.get("alias") not in activated_aliases
            )

            # Get recent activity (recent syncs)
            recent_syncs = []
            for repo in activated_repos:
                try:
                    last_sync_str = repo.get("last_accessed")
                    if last_sync_str:
                        last_sync = datetime.fromisoformat(
                            last_sync_str.replace("Z", "+00:00")
                        )
                        if last_sync > datetime.now(timezone.utc) - timedelta(days=7):
                            sync_status = (
                                "success"
                                if repo.get("sync_status") == "synced"
                                else "failed"
                            )
                            recent_syncs.append(
                                {
                                    "alias": repo.get("user_alias"),
                                    "sync_date": last_sync_str,
                                    "status": sync_status,
                                }
                            )
                except (ValueError, AttributeError):
                    pass

            # Generate recommendations
            recommendations = []
            if total_activated == 0:
                recommendations.append(
                    "No repositories activated yet. Use 'cidx repos available' to browse and activate repositories."
                )
            else:
                if needs_sync_count > 0:
                    recommendations.append(
                        f"{needs_sync_count} repositories need synchronization. Use 'cidx repos sync' to update them."
                    )
                if conflict_count > 0:
                    recommendations.append(
                        f"{conflict_count} repositories have conflicts that need manual resolution."
                    )
                if not_activated_count > 0:
                    recommendations.append(
                        f"{not_activated_count} repositories are available for activation."
                    )

            # Create response
            return RepositoryStatusSummary(
                activated_repositories=ActivatedRepositorySummary(
                    total_count=total_activated,
                    synced_count=synced_count,
                    needs_sync_count=needs_sync_count,
                    conflict_count=conflict_count,
                    recent_activations=recent_activations[
                        -5:
                    ],  # Last 5 recent activations
                ),
                available_repositories=AvailableRepositorySummary(
                    total_count=total_available, not_activated_count=not_activated_count
                ),
                recent_activity=RecentActivity(
                    recent_syncs=recent_syncs[-10:]  # Last 10 recent syncs
                ),
                recommendations=recommendations,
            )

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get repository status: {str(e)}",
            )

    # Repository Information Endpoint (Story 6) - generic route MUST be last
    @app.get("/api/repos/{user_alias}")
    def get_repository_info(
        user_alias: str,
        branches: Optional[bool] = Query(
            False, description="Include branch information"
        ),
        health: Optional[bool] = Query(False, description="Include health monitoring"),
        activity: Optional[bool] = Query(
            False, description="Include activity tracking"
        ),
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get comprehensive repository information with optional detailed sections.

        Supports query parameters for selective information retrieval:
        - ?branches=true: Include detailed branch information
        - ?health=true: Include health monitoring information
        - ?activity=true: Include activity tracking information

        Following CLAUDE.md Foundation #1: No mocks - real repository data.
        """
        try:
            # Validate user_alias format
            if not user_alias or not user_alias.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Repository alias cannot be empty",
                )

            cleaned_alias = user_alias.strip()

            # Check if repository exists in user's activated repositories
            activated_repos = activated_repo_manager.list_activated_repositories(
                current_user.username
            )

            repo_found = None
            for repo in activated_repos:
                if repo["user_alias"] == cleaned_alias:
                    repo_found = repo
                    break

            if not repo_found:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Repository '{cleaned_alias}' not found or not activated",
                )

            # Get repository path
            repo_path = activated_repo_manager.get_activated_repo_path(
                current_user.username, cleaned_alias
            )

            # Build basic repository information
            result = {
                "alias": cleaned_alias,
                "git_url": repo_found.get("git_url", ""),
                "activation_date": repo_found.get("activation_date", ""),
                "sync_status": repo_found.get("sync_status", "unknown"),
                "last_sync": repo_found.get("last_sync"),
                "golden_repository": repo_found.get("golden_repository", ""),
            }

            # Get current branch
            try:
                current_branch = activated_repo_manager.get_current_branch(
                    current_user.username, cleaned_alias
                )
                result["current_branch"] = current_branch
            except Exception:
                result["current_branch"] = "unknown"

            # Add basic status information
            result["container_status"] = "unknown"
            result["index_status"] = "unknown"
            result["query_ready"] = False

            # Add storage information
            storage_info = {}
            if os.path.exists(repo_path):
                try:
                    # Calculate repository size
                    total_size = 0
                    for root, dirs, files in os.walk(repo_path):
                        if ".git" in dirs:
                            dirs.remove(".git")
                        for file in files:
                            if not file.startswith("."):
                                file_path = os.path.join(root, file)
                                try:
                                    total_size += os.path.getsize(file_path)
                                except (OSError, IOError):
                                    continue

                    storage_info["disk_usage_mb"] = round(total_size / (1024 * 1024), 2)

                    # Calculate index size if exists
                    index_path = os.path.join(repo_path, ".code-indexer")
                    if os.path.exists(index_path):
                        index_size = 0
                        for root, dirs, files in os.walk(index_path):
                            for file in files:
                                try:
                                    index_size += os.path.getsize(
                                        os.path.join(root, file)
                                    )
                                except (OSError, IOError):
                                    continue
                        storage_info["index_size_mb"] = round(
                            index_size / (1024 * 1024), 2
                        )
                except Exception:
                    storage_info["disk_usage_mb"] = 0
                    storage_info["index_size_mb"] = 0

            result["storage_info"] = storage_info

            # Add detailed sections based on query parameters
            if branches:
                try:
                    branch_info = activated_repo_manager.list_repository_branches(
                        current_user.username, cleaned_alias
                    )

                    # Format branches for client
                    formatted_branches = []
                    for branch_name in branch_info.get("branches", []):
                        is_current = branch_name == result.get("current_branch")
                        formatted_branches.append(
                            {
                                "name": branch_name,
                                "is_current": is_current,
                                "last_commit": {
                                    "message": "commit message unavailable",
                                    "timestamp": "unknown",
                                    "author": "unknown",
                                },
                            }
                        )

                    result["branches"] = formatted_branches
                except Exception:
                    result["branches"] = []

            if health:
                health_info: Dict[str, Any] = {
                    "container_status": "unknown",
                    "services": {},
                    "index_status": "unknown",
                    "query_ready": False,
                    "storage": storage_info,
                    "issues": [],
                    "recommendations": [],
                }

                # Container status check removed (Story #506: container management deprecated)
                # Filesystem backend is always ready
                try:
                    from ...config import ConfigManager

                    config_manager = ConfigManager.create_with_backtrack(
                        Path(repo_path)
                    )
                    config = config_manager.get_config()

                    # Check if using filesystem backend
                    is_filesystem = (
                        hasattr(config, "vector_store")
                        and config.vector_store
                        and config.vector_store.provider == "filesystem"
                    )

                    if is_filesystem:
                        health_info["container_status"] = "not_applicable"
                        health_info["query_ready"] = True

                        # Filesystem backend doesn't have service dependencies
                        health_info["services"]["vector_store"] = {
                            "status": "healthy",
                            "type": "filesystem",
                        }
                    else:
                        health_info["container_status"] = "stopped"
                        health_info["recommendations"].append(
                            "Containers are stopped. Query operations will auto-start them."
                        )

                except Exception:
                    health_info["issues"].append("Unable to determine container status")

                result["health"] = health_info

            if activity:
                activity_info: Dict[str, Any] = {
                    "recent_commits": [],
                    "sync_history": [],
                    "query_activity": {"recent_queries": 0, "last_query": None},
                    "branch_operations": [],
                }

                # Try to get real git commit history
                try:
                    import subprocess

                    git_log = subprocess.run(
                        ["git", "log", "--oneline", "-5"],
                        cwd=repo_path,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )

                    if git_log.returncode == 0:
                        for line in git_log.stdout.strip().split("\n"):
                            if line.strip():
                                parts = line.split(" ", 1)
                                if len(parts) >= 2:
                                    commit_hash = parts[0]
                                    message = parts[1]
                                    activity_info["recent_commits"].append(
                                        {
                                            "commit_hash": commit_hash,
                                            "message": message,
                                            "author": "unknown",
                                            "timestamp": "unknown",
                                        }
                                    )
                except Exception:
                    pass  # Git history unavailable

                # Add sync history from repository metadata
                if repo_found.get("last_sync"):
                    activity_info["sync_history"].append(
                        {
                            "timestamp": repo_found["last_sync"],
                            "status": "success",
                            "changes": "sync details unavailable",
                        }
                    )

                result["activity"] = activity_info

            return result

        except HTTPException:
            raise
        except Exception as e:
            logging.error(f"Failed to get repository info for {user_alias}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve repository information: {str(e)}",
            )
