"""
Admin operations route handlers extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 12 route handlers:
- POST /api/admin/scip-cleanup-workspaces
- GET /api/admin/scip-cleanup-status
- GET /api/admin/golden-repos
- POST /api/admin/golden-repos
- POST /api/admin/golden-repos/{alias}/refresh
- POST /api/admin/golden-repos/{alias}/indexes
- GET /api/admin/golden-repos/{alias}/indexes
- DELETE /api/admin/jobs/cleanup
- GET /api/admin/jobs/stats
- GET /api/admin/scip-pr-history
- GET /api/admin/scip-git-cleanup-history
- DELETE /api/admin/golden-repos/{alias}

Zero behavior change: same paths, methods, response models, and handler logic.
"""

import logging
from typing import Dict, Any, Optional

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Response,
    Request,
)
from fastapi.responses import JSONResponse

from ..models.repos import (
    AddGoldenRepoRequest,
)
from ..models.jobs import (
    AddIndexRequest,
    AddIndexResponse,
    IndexInfo,
    IndexStatusResponse,
    JobResponse,
    JobCleanupResponse,
)

from ..auth import dependencies
from ..repositories.golden_repo_manager import GoldenRepoError, GitOperationError
from ..repositories.background_jobs import DuplicateJobError
from ..logging_utils import format_error_log
from ..middleware.correlation import get_correlation_id

# Constants used by route handlers
GOLDEN_REPO_ADD_OPERATION = "add_golden_repo"
GOLDEN_REPO_REFRESH_OPERATION = "refresh_golden_repo"
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"

# Module-level logger
logger = logging.getLogger(__name__)


def register_admin_ops_routes(
    app: FastAPI,
    *,
    jwt_manager,
    user_manager,
    golden_repo_manager,
    background_job_manager,
    workspace_cleanup_service,
    config_service,
    server_config,
    data_dir: str,
    job_tracker,
) -> None:
    """
    Register admin operations route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures in inline_routes.py. No handler logic is changed.

    Args:
        app: The FastAPI application instance
        jwt_manager: JWTManager instance
        user_manager: UserManager instance
        golden_repo_manager: GoldenRepoManager instance
        background_job_manager: BackgroundJobManager instance
        workspace_cleanup_service: WorkspaceCleanupService instance
        config_service: ConfigService instance
        server_config: ServerConfig instance
        data_dir: Server data directory path
        job_tracker: JobTracker instance
    """

    @app.post("/api/admin/scip-cleanup-workspaces")
    def scip_cleanup_workspaces(
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Manually trigger SCIP workspace cleanup (Story #647 - AC4).

        Triggers immediate cleanup of expired SCIP self-healing workspaces using
        WorkspaceCleanupService. Returns cleanup summary with counts and space reclaimed.

        Args:
            current_user: Current authenticated admin user

        Returns:
            Cleanup summary with deleted_count, preserved_count, space_freed_mb, errors

        Raises:
            HTTPException: If cleanup service unavailable or cleanup fails
        """
        try:
            result = workspace_cleanup_service.cleanup_workspaces()

            # Convert space from bytes to MB
            space_freed_mb = result.space_reclaimed_bytes / (1024 * 1024)

            return {
                "deleted_count": result.workspaces_deleted,
                "preserved_count": result.workspaces_preserved,
                "space_freed_mb": round(space_freed_mb, 2),
                "errors": result.errors,
            }

        except Exception as e:
            logger.error(
                format_error_log(
                    "APP-GENERAL-030",
                    f"SCIP workspace cleanup failed: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Workspace cleanup failed: {str(e)}",
            )

    @app.get("/api/admin/scip-cleanup-status")
    def get_scip_cleanup_status(
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Get SCIP workspace cleanup status (Story #647 - AC5).

        Returns current cleanup status including last cleanup time, workspace count,
        oldest workspace age, and total workspace size.

        Args:
            current_user: Current authenticated admin user

        Returns:
            Status dictionary with last_cleanup_time, workspace_count,
            oldest_workspace_age, total_size_mb

        Raises:
            HTTPException: If cleanup service unavailable or status retrieval fails
        """
        try:
            status_info = workspace_cleanup_service.get_cleanup_status()
            return status_info

        except Exception as e:
            logger.error(
                format_error_log(
                    "APP-GENERAL-031",
                    f"Failed to get SCIP cleanup status: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get cleanup status: {str(e)}",
            )

    @app.get("/api/admin/golden-repos")
    def list_golden_repos(
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        List all golden repositories (admin only).

        Returns:
            List of golden repositories
        """
        repos = golden_repo_manager.list_golden_repos()
        return {
            "golden_repositories": repos,
            "total": len(repos),
        }

    @app.post("/api/admin/golden-repos", response_model=JobResponse, status_code=202)
    def add_golden_repo(
        repo_data: AddGoldenRepoRequest,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Add a golden repository (admin only) - async operation.

        Args:
            repo_data: Golden repository data
            current_user: Current authenticated admin user

        Returns:
            Job ID and message for tracking the async operation

        Raises:
            HTTPException: If job submission fails
        """
        try:
            # Submit background job for adding golden repo
            func_kwargs: Dict[str, Any] = {
                "repo_url": repo_data.repo_url,
                "alias": repo_data.alias,
                "default_branch": repo_data.default_branch,
                "description": repo_data.description,
                "enable_temporal": repo_data.enable_temporal,
            }

            # Add temporal_options if provided
            if repo_data.temporal_options:
                func_kwargs["temporal_options"] = (
                    repo_data.temporal_options.model_dump()
                )

            job_id = golden_repo_manager.add_golden_repo(
                submitter_username=current_user.username,
                **func_kwargs,  # type: ignore[arg-type]
            )
            return JobResponse(
                job_id=job_id,
                message=f"Golden repository '{repo_data.alias}' addition started",
            )

        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to submit job: {str(e)}",
            )

    @app.post(
        "/api/admin/golden-repos/{alias}/refresh",
        response_model=JobResponse,
        status_code=202,
    )
    def refresh_golden_repo(
        alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Refresh a golden repository (admin only) - async operation.

        Args:
            alias: Alias of the repository to refresh
            current_user: Current authenticated admin user

        Returns:
            Job ID and message for tracking the async operation

        Raises:
            HTTPException: If job submission fails
        """
        try:
            # Validate repo exists before scheduling
            if alias not in golden_repo_manager.golden_repos:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Golden repository '{alias}' not found",
                )
            # Delegate to RefreshScheduler (index-source-first versioned pipeline)
            lifecycle_manager = getattr(app.state, "global_lifecycle_manager", None)
            if not lifecycle_manager or not lifecycle_manager.refresh_scheduler:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="RefreshScheduler not available",
                )
            # Resolution from bare alias to global format happens inside RefreshScheduler
            job_id = lifecycle_manager.refresh_scheduler.trigger_refresh_for_repo(
                alias, submitter_username=current_user.username
            )
            return JobResponse(
                job_id=job_id or "",
                message=f"Golden repository '{alias}' refresh started",
            )

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to submit refresh job: {str(e)}",
            )

    @app.post(
        "/api/admin/golden-repos/{alias}/indexes",
        response_model=AddIndexResponse,
        status_code=202,
    )
    def add_golden_repo_index(
        http_request: Request,
        alias: str,
        request: AddIndexRequest,
        current_user: dependencies.User = Depends(
            dependencies.get_current_admin_user_hybrid
        ),
    ):
        """
        Add index type(s) to a golden repository (admin only) - async operation.

        Supports both single and multi-select modes:
        - Single: { "index_type": "semantic" } - backward compatible
        - Multi: { "index_types": ["semantic", "fts", "temporal"] }

        Args:
            alias: Alias of the golden repository
            request: AddIndexRequest with index_type or index_types
            current_user: Current authenticated admin user

        Returns:
            AddIndexResponse with job_id (single) or job_ids (multi)

        Raises:
            HTTPException 404: If golden repository not found
            HTTPException 400: If invalid index_type(s)
            HTTPException 409: If index already exists
            HTTPException 500: If job submission fails
        """

        # Get list of index types to process
        index_types = request.get_index_types()

        # Validate all index types first before submitting any jobs
        valid_index_types = ["semantic", "fts", "temporal", "scip"]
        invalid_types = [t for t in index_types if t not in valid_index_types]
        if invalid_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid index_type(s): {', '.join(invalid_types)}. Must be one of: {', '.join(valid_index_types)}",
            )

        try:
            job_ids = []
            repo_path: Optional[str] = None  # resolved lazily by provider blocks below

            # Story #489: When providers are specified and semantic is requested,
            # submit per-provider jobs instead of the generic semantic job.
            remaining_index_types = list(index_types)
            if request.providers and "semantic" in remaining_index_types:
                from ..mcp.handlers import (
                    _provider_index_job,
                    _resolve_golden_repo_path,
                    _resolve_golden_repo_base_clone,
                    _append_provider_to_config,
                )

                remaining_index_types = [
                    t for t in remaining_index_types if t != "semantic"
                ]
                repo_path = _resolve_golden_repo_path(alias)
                if repo_path is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Golden repository '{alias}' not found or path not resolvable",
                    )

                # Bug #625: Write provider to base clone config before submitting job.
                # Bug #648/#4: Append ALL providers to config first, then submit ONE job.
                # The CLI handles all providers in sequence; submitting N jobs caused
                # the 2nd+ to be silently dropped (same operation_type conflict) or race.
                base_clone = _resolve_golden_repo_base_clone(alias)

                if base_clone:
                    for provider_name in request.providers:
                        _append_provider_to_config(base_clone, provider_name)

                provider_job_id = background_job_manager.submit_job(
                    operation_type="provider_index_add",
                    func=_provider_index_job,
                    submitter_username=current_user.username,
                    repo_alias=alias,
                    repo_path=repo_path,
                    provider_name=request.providers[0],
                    clear=False,
                )
                job_ids.append(provider_job_id)

            # Story #641: Per-provider temporal jobs (same pattern as semantic).
            if request.providers and "temporal" in remaining_index_types:
                from ..mcp.handlers import (
                    _provider_temporal_index_job,
                    _resolve_golden_repo_path,
                    _resolve_golden_repo_base_clone,
                    _append_provider_to_config,
                )

                remaining_index_types = [
                    t for t in remaining_index_types if t != "temporal"
                ]
                if repo_path is None:
                    repo_path = _resolve_golden_repo_path(alias)
                if repo_path is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Golden repository '{alias}' not found",
                    )

                base_clone = _resolve_golden_repo_base_clone(alias)

                # Read temporal_options here — golden_repo_manager is accessible in the
                # route handler but NOT inside background job workers (Story #641).
                _temporal_opts: dict = {}
                _repo_meta = golden_repo_manager.golden_repos.get(alias)
                if _repo_meta and getattr(_repo_meta, "temporal_options", None):
                    _temporal_opts = _repo_meta.temporal_options

                # Bug #648/#3: Append ALL providers to config first, then submit ONE job.
                # The CLI (cidx index --index-commits) handles all providers in sequence.
                # Submitting N concurrent jobs causes HNSW + SQLite temporal metadata races
                # that corrupt the index (FileNotFoundError on atomic rename).
                if base_clone:
                    for provider_name in request.providers:
                        _append_provider_to_config(base_clone, provider_name)

                provider_job_id = background_job_manager.submit_job(
                    operation_type="provider_temporal_index_rebuild",
                    func=_provider_temporal_index_job,
                    submitter_username=current_user.username,
                    repo_alias=alias,
                    repo_path=repo_path,
                    provider_name=request.providers[0],
                    clear=False,
                    temporal_options=_temporal_opts,
                )
                job_ids.append(provider_job_id)

            # Submit combined job for remaining non-semantic index types (Bug #473 fix: atomic).
            if remaining_index_types:
                job_id = golden_repo_manager.add_indexes_to_golden_repo(
                    alias=alias,
                    index_types=remaining_index_types,
                    submitter_username=current_user.username,
                )
                job_ids.append(job_id)

            if not job_ids:
                # No jobs submitted (e.g. providers list was provided but empty after all)
                raise HTTPException(
                    status_code=400,
                    detail="No index jobs could be submitted. Verify providers and index types.",
                )

            # Return first job_id for backward compat; all job_ids for multi-select consumers.
            response = AddIndexResponse(
                job_id=job_ids[0], job_ids=job_ids, status="pending"
            )
            return JSONResponse(
                content=response.model_dump(),
                status_code=202,
                headers={"Location": f"/api/jobs/{job_ids[0]}"},
            )

        except DuplicateJobError as e:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(e),
            )
        except ValueError as e:
            error_msg = str(e)
            # Determine if it's 404 (not found) or 409 (conflict) or 400 (invalid)
            if "not found" in error_msg.lower():
                raise HTTPException(status_code=404, detail=error_msg)
            elif "already exists" in error_msg.lower():
                raise HTTPException(status_code=409, detail=error_msg)
            elif "invalid index_type" in error_msg.lower():
                raise HTTPException(status_code=400, detail=error_msg)
            else:
                raise HTTPException(status_code=400, detail=error_msg)
        except HTTPException:
            raise  # Bug #605: don't swallow HTTPException(404) as a 500
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to submit job: {str(e)}",
            )

    @app.get(
        "/api/admin/golden-repos/{alias}/indexes",
        response_model=IndexStatusResponse,
    )
    def get_golden_repo_index_status(
        alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Get index status for a golden repository (admin only).

        Args:
            alias: Alias of the golden repository
            current_user: Current authenticated admin user

        Returns:
            IndexStatusResponse with alias and index presence information

        Raises:
            HTTPException 404: If golden repository not found
        """
        # Check if golden repo exists
        golden_repo = golden_repo_manager.get_golden_repo(alias)
        if golden_repo is None:
            raise HTTPException(
                status_code=404, detail=f"Golden repository '{alias}' not found"
            )

        # Query index presence for all four individual types
        indexes = {}
        for index_type in ["semantic", "fts", "temporal", "scip"]:
            present = golden_repo_manager._index_exists(golden_repo, index_type)
            indexes[index_type] = IndexInfo(present=present)

        return IndexStatusResponse(alias=alias, indexes=indexes)

    @app.delete("/api/admin/jobs/cleanup", response_model=JobCleanupResponse)
    def cleanup_old_jobs(
        max_age_hours: Optional[int] = None,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Clean up old completed/failed jobs (admin only).

        Args:
            max_age_hours: Maximum age of jobs to keep in hours.
                If not provided, uses the configured default from
                data_retention_config.background_jobs_retention_hours (default: 720, i.e. 30 days).
            current_user: Current authenticated admin user

        Returns:
            Number of jobs cleaned up
        """
        # Story #400 - AC5: Use DataRetentionConfig.background_jobs_retention_hours
        if max_age_hours is None:
            from code_indexer.server.services.config_service import get_config_service

            _config_service = get_config_service()
            max_age_hours = _config_service.get_config().data_retention_config.background_jobs_retention_hours
        if max_age_hours < 1:
            max_age_hours = 1
        if max_age_hours > 8760:  # 1 year
            max_age_hours = 8760

        cleaned_count = background_job_manager.cleanup_old_jobs(
            max_age_hours=max_age_hours
        )

        return JobCleanupResponse(
            cleaned_count=cleaned_count,
            message=f"Cleaned up {cleaned_count} old background jobs",
        )

    @app.get("/api/admin/jobs/stats")
    def admin_jobs_stats(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """Get job statistics for admin dashboard."""
        from datetime import datetime

        # Get all jobs from the background job manager
        all_jobs = list(background_job_manager.jobs.values())

        # Filter by date range if provided
        filtered_jobs = all_jobs
        if start_date or end_date:
            start_dt = datetime.fromisoformat(start_date) if start_date else None
            end_dt = datetime.fromisoformat(end_date) if end_date else None

            filtered_jobs = [
                job
                for job in all_jobs
                if (not start_dt or job.created_at >= start_dt)
                and (not end_dt or job.created_at <= end_dt)
            ]

        # Calculate statistics
        total_jobs = len(filtered_jobs)

        # Count by status
        by_status: Dict[str, int] = {}
        for job in filtered_jobs:
            status_val = job.status.value
            by_status[status_val] = by_status.get(status_val, 0) + 1

        # Count by type
        by_type: Dict[str, int] = {}
        for job in filtered_jobs:
            job_type = job.operation_type
            by_type[job_type] = by_type.get(job_type, 0) + 1

        # Calculate success rate
        completed_jobs = by_status.get("completed", 0)
        failed_jobs = by_status.get("failed", 0)
        total_finished = completed_jobs + failed_jobs
        success_rate = (
            (completed_jobs / total_finished * 100.0) if total_finished > 0 else 0.0
        )

        # Calculate average duration for completed jobs
        durations = []
        for job in filtered_jobs:
            if job.completed_at and job.started_at:
                duration = (job.completed_at - job.started_at).total_seconds()
                durations.append(duration)

        average_duration = sum(durations) / len(durations) if durations else 0.0

        return {
            "total_jobs": total_jobs,
            "by_status": by_status,
            "by_type": by_type,
            "success_rate": success_rate,
            "average_duration": average_duration,
        }

    @app.get("/api/admin/scip-pr-history")
    def get_scip_pr_history(
        repo_alias: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Get SCIP PR creation audit history (admin only).

        Args:
            repo_alias: Filter by repository alias (optional)
            limit: Maximum number of records to return (default: 100)
            offset: Number of records to skip (default: 0)

        Returns:
            Audit log entries for PR creations
        """
        try:
            logs = app.state.audit_service.get_pr_logs(
                repo_alias=repo_alias, limit=limit, offset=offset
            )
            return {"logs": logs, "total": len(logs)}
        except Exception as e:
            logger.error("Error fetching PR history: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/admin/scip-git-cleanup-history")
    def get_scip_git_cleanup_history(
        repo_path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Get SCIP git cleanup audit history (admin only).

        Args:
            repo_path: Filter by repository path (optional)
            limit: Maximum number of records to return (default: 100)
            offset: Number of records to skip (default: 0)

        Returns:
            Audit log entries for git cleanup operations
        """
        try:
            logs = app.state.audit_service.get_cleanup_logs(
                repo_path=repo_path, limit=limit, offset=offset
            )
            return {"logs": logs, "total": len(logs)}
        except Exception as e:
            logger.error("Error fetching git cleanup history: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/api/admin/golden-repos/{alias}", status_code=204)
    def remove_golden_repo(
        alias: str,
        current_user: dependencies.User = Depends(dependencies.get_current_admin_user),
    ):
        """
        Remove a golden repository (admin only).

        This endpoint implements comprehensive repository deletion with:
        - Proper HTTP 204 No Content response for successful deletions
        - Transaction management with rollback on failures
        - Graceful cancellation of active background jobs
        - Comprehensive resource cleanup in finally blocks
        - Proper error categorization and sanitized error messages
        - Protection against broken pipe errors and resource leaks

        Args:
            alias: Alias of the repository to remove
            current_user: Current authenticated admin user

        Returns:
            No content (HTTP 204) on successful deletion

        Raises:
            HTTPException: 404 if repository not found, 503 if services unavailable, 500 for other errors
        """
        try:
            # Cancel any active background jobs for this repository
            try:
                if background_job_manager:
                    # Get jobs related to this golden repository
                    active_jobs = (
                        background_job_manager.get_jobs_by_operation_and_params(
                            operation_types=[
                                GOLDEN_REPO_ADD_OPERATION,
                                GOLDEN_REPO_REFRESH_OPERATION,
                                "global_repo_refresh",
                            ],
                            params_filter={"alias": alias},
                        )
                    )

                    # Cancel active jobs gracefully
                    for job in active_jobs:
                        if job.get("status") in [
                            JOB_STATUS_PENDING,
                            JOB_STATUS_RUNNING,
                        ]:
                            cancel_result = background_job_manager.cancel_job(
                                job["job_id"], current_user.username
                            )
                            if cancel_result["success"]:
                                logging.info(
                                    f"Cancelled background job {job['job_id']} for repository {alias}"
                                )
                            else:
                                logging.warning(
                                    f"Failed to cancel job {job['job_id']}: {cancel_result['message']}"
                                )
            except Exception as job_error:
                # Job cancellation failure shouldn't prevent deletion, but log it
                logging.warning(
                    f"Job cancellation failed during repository deletion: {job_error}"
                )

            # Perform repository deletion with proper error handling
            golden_repo_manager.remove_golden_repo(alias)

            logging.info(
                f"Successfully removed golden repository '{alias}' by user '{current_user.username}'"
            )

            # Return 204 No Content (no response body)
            return Response(status_code=204)

        except GitOperationError as e:
            error_msg = str(e).lower()

            # Categorize GitOperationError by underlying cause
            if any(
                service_term in error_msg
                for service_term in [
                    "service unavailable",
                    "connection timeout",
                ]
            ):
                # External service issues - return 503 Service Unavailable
                sanitized_message = "Repository deletion failed due to service unavailability. Please try again later."
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=sanitized_message,
                )
            elif "broken pipe" in error_msg:
                # Sanitize broken pipe errors - don't expose internal details
                sanitized_message = "Repository deletion failed due to internal communication error. The operation may have completed partially."
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=sanitized_message,
                )
            else:
                # Other GitOperationErrors (permission, filesystem, etc.) - return 500
                # Keep more detail for compatibility with existing tests
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=str(e),  # Preserve original error message
                )

        except GoldenRepoError as e:
            # Repository not found - return 404
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(e),  # Safe to expose - just "repository not found"
            )

        except Exception as e:
            # Unexpected errors - return 500 with sanitized message
            logging.error(f"Unexpected error during repository deletion: {e}")
            detail_message = f"Failed to remove repository: {str(e)}"
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail_message,
            )
