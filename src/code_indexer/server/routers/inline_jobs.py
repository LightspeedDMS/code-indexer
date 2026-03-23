"""
Job management route handlers extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 3 route handlers:
- GET /api/jobs/{job_id}
- GET /api/jobs
- DELETE /api/jobs/{job_id}

Zero behavior change: same paths, methods, response models, and handler logic.
"""

import logging
from typing import Optional

from fastapi import (
    FastAPI,
    HTTPException,
    status,
    Depends,
    Request,
)

from ..models.jobs import (
    JobStatusResponse,
    JobListResponse,
    JobCancellationResponse,
)

from ..auth import dependencies

# Module-level logger
logger = logging.getLogger(__name__)


def register_job_routes(
    app: FastAPI,
    *,
    jwt_manager,
    user_manager,
    background_job_manager,
    job_tracker,
) -> None:
    """
    Register job management route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures over create_app() locals before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        jwt_manager: JWTManager instance
        user_manager: UserManager instance
        background_job_manager: BackgroundJobManager instance
        job_tracker: JobTracker instance
    """

    @app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
    def get_job_status(
        http_request: Request,
        job_id: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user_hybrid),
    ):
        """
        Get status of a background job.

        Args:
            job_id: Job ID to check status for
            current_user: Current authenticated user

        Returns:
            Job status information

        Raises:
            HTTPException: If job not found
        """
        job_status = background_job_manager.get_job_status(
            job_id, current_user.username
        )
        if not job_status:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job not found: {job_id}",
            )

        return JobStatusResponse(
            job_id=job_status["job_id"],
            operation_type=job_status["operation_type"],
            status=job_status["status"],
            created_at=job_status["created_at"],
            started_at=job_status["started_at"],
            completed_at=job_status["completed_at"],
            progress=job_status["progress"],
            result=job_status["result"],
            error=job_status["error"],
            username=job_status["username"],
            # Story #480: Real-time phase progress fields (AC7)
            current_phase=job_status.get("current_phase"),
            phase_detail=job_status.get("phase_detail"),
        )

    @app.get("/api/jobs", response_model=JobListResponse)
    def list_jobs(
        status: Optional[str] = None,
        limit: int = 10,
        offset: int = 0,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        List jobs for current user with filtering and pagination.

        Args:
            status: Filter jobs by status (pending, running, completed, failed, cancelled)
            limit: Maximum number of jobs to return (default: 10, max: 100)
            offset: Number of jobs to skip (default: 0)
            current_user: Current authenticated user

        Returns:
            List of jobs with pagination metadata
        """
        # Validate limit
        if limit > 100:
            limit = 100
        if limit < 1:
            limit = 1

        if offset < 0:
            offset = 0

        job_list = background_job_manager.list_jobs(
            username=current_user.username,
            status_filter=status,
            limit=limit,
            offset=offset,
        )

        # Convert job data to response models
        jobs = []
        for job_data in job_list["jobs"]:
            jobs.append(
                JobStatusResponse(
                    job_id=job_data["job_id"],
                    operation_type=job_data["operation_type"],
                    status=job_data["status"],
                    created_at=job_data["created_at"],
                    started_at=job_data["started_at"],
                    completed_at=job_data["completed_at"],
                    progress=job_data["progress"],
                    result=job_data["result"],
                    error=job_data["error"],
                    username=job_data["username"],
                )
            )

        return JobListResponse(
            jobs=jobs,
            total=job_list["total"],
            limit=job_list["limit"],
            offset=job_list["offset"],
        )

    @app.delete("/api/jobs/{job_id}", response_model=JobCancellationResponse)
    def cancel_job(
        job_id: str,
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Cancel a background job.

        Args:
            job_id: Job ID to cancel
            current_user: Current authenticated user

        Returns:
            Cancellation result

        Raises:
            HTTPException: If job not found, not authorized, or cannot be cancelled
        """
        result = background_job_manager.cancel_job(job_id, current_user.username)

        if not result["success"]:
            if "not found or not authorized" in result["message"]:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail=result["message"]
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=result["message"]
                )

        return JobCancellationResponse(
            success=result["success"], message=result["message"]
        )
