"""
CI/CD Monitoring REST API Routes.

Story #745: CI/CD Monitoring REST Endpoints

Provides 12 REST API endpoints for CI/CD monitoring:
- 6 GitHub Actions endpoints
- 6 GitLab CI endpoints

All endpoints require authentication and delegate to existing MCP handlers.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Path, Query

from ..auth.dependencies import get_current_user
from ..auth.user_manager import User
from ..mcp.handlers import (
    handle_gh_actions_list_runs,
    handle_gh_actions_get_run,
    handle_gh_actions_search_logs,
    handle_gh_actions_get_job_logs,
    handle_gh_actions_retry_run,
    handle_gh_actions_cancel_run,
    handle_gitlab_ci_list_pipelines,
    handle_gitlab_ci_get_pipeline,
    handle_gitlab_ci_search_logs,
    handle_gitlab_ci_get_job_logs,
    handle_gitlab_ci_retry_pipeline,
    handle_gitlab_ci_cancel_pipeline,
)


# Create router with /api/cicd prefix
router = APIRouter(prefix="/api/cicd", tags=["cicd"])


# =============================================================================
# GitHub Actions Endpoints (6 total)
# =============================================================================


@router.get("/github/{owner}/{repo}/runs")
async def github_list_runs(
    owner: str = Path(..., description="Repository owner"),
    repo: str = Path(..., description="Repository name"),
    status: Optional[str] = Query(None, description="Filter by run status"),
    branch: Optional[str] = Query(None, description="Filter by branch name"),
    limit: Optional[int] = Query(10, description="Maximum runs to return"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    List GitHub Actions workflow runs for a repository.

    Returns workflow runs with optional filtering by status and branch.
    """
    args = {
        "repository": f"{owner}/{repo}",
        "status": status,
        "branch": branch,
        "limit": limit,
    }
    return await handle_gh_actions_list_runs(args, user)


@router.get("/github/{owner}/{repo}/runs/{run_id}")
async def github_get_run(
    owner: str = Path(..., description="Repository owner"),
    repo: str = Path(..., description="Repository name"),
    run_id: int = Path(..., description="Workflow run ID"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get detailed information about a specific GitHub Actions workflow run.

    Returns run details including status, jobs, and timing information.
    """
    args = {
        "repository": f"{owner}/{repo}",
        "run_id": run_id,
    }
    return await handle_gh_actions_get_run(args, user)


@router.get("/github/{owner}/{repo}/runs/{run_id}/logs")
async def github_search_logs(
    owner: str = Path(..., description="Repository owner"),
    repo: str = Path(..., description="Repository name"),
    run_id: int = Path(..., description="Workflow run ID"),
    query: Optional[str] = Query(None, description="Search pattern for logs"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Search logs for a GitHub Actions workflow run.

    Returns matching log lines with job and step context.
    """
    args = {
        "repository": f"{owner}/{repo}",
        "run_id": run_id,
        "pattern": query or ".*",  # Default to match all if no query
    }
    return await handle_gh_actions_search_logs(args, user)


@router.get("/github/{owner}/{repo}/jobs/{job_id}/logs")
async def github_get_job_logs(
    owner: str = Path(..., description="Repository owner"),
    repo: str = Path(..., description="Repository name"),
    job_id: int = Path(..., description="Job ID"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get complete logs for a specific GitHub Actions job.

    Returns the full log output for the specified job.
    """
    args = {
        "repository": f"{owner}/{repo}",
        "job_id": job_id,
    }
    return await handle_gh_actions_get_job_logs(args, user)


@router.post("/github/{owner}/{repo}/runs/{run_id}/retry")
async def github_retry_run(
    owner: str = Path(..., description="Repository owner"),
    repo: str = Path(..., description="Repository name"),
    run_id: int = Path(..., description="Workflow run ID to retry"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Retry a failed GitHub Actions workflow run.

    Triggers a re-run of the specified workflow run.
    """
    args = {
        "repository": f"{owner}/{repo}",
        "run_id": run_id,
    }
    return await handle_gh_actions_retry_run(args, user)


@router.post("/github/{owner}/{repo}/runs/{run_id}/cancel")
async def github_cancel_run(
    owner: str = Path(..., description="Repository owner"),
    repo: str = Path(..., description="Repository name"),
    run_id: int = Path(..., description="Workflow run ID to cancel"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Cancel a running or queued GitHub Actions workflow run.

    Stops the specified workflow run if it is in progress or queued.
    """
    args = {
        "repository": f"{owner}/{repo}",
        "run_id": run_id,
    }
    return await handle_gh_actions_cancel_run(args, user)


# =============================================================================
# GitLab CI Endpoints (6 total)
# =============================================================================


@router.get("/gitlab/{project_id}/pipelines")
async def gitlab_list_pipelines(
    project_id: str = Path(..., description="GitLab project ID or path"),
    status: Optional[str] = Query(None, description="Filter by pipeline status"),
    ref: Optional[str] = Query(None, description="Filter by branch/tag name"),
    limit: Optional[int] = Query(10, description="Maximum pipelines to return"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    List GitLab CI pipelines for a project.

    Returns pipelines with optional filtering by status and ref.
    """
    args = {
        "project_id": project_id,
        "status": status,
        "ref": ref,
        "limit": limit,
    }
    return await handle_gitlab_ci_list_pipelines(args, user)


@router.get("/gitlab/{project_id}/pipelines/{pipeline_id}")
async def gitlab_get_pipeline(
    project_id: str = Path(..., description="GitLab project ID or path"),
    pipeline_id: int = Path(..., description="Pipeline ID"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get detailed information about a specific GitLab CI pipeline.

    Returns pipeline details including status, jobs, and timing information.
    """
    args = {
        "project_id": project_id,
        "pipeline_id": pipeline_id,
    }
    return await handle_gitlab_ci_get_pipeline(args, user)


@router.get("/gitlab/{project_id}/pipelines/{pipeline_id}/logs")
async def gitlab_search_logs(
    project_id: str = Path(..., description="GitLab project ID or path"),
    pipeline_id: int = Path(..., description="Pipeline ID"),
    query: Optional[str] = Query(None, description="Search pattern for logs"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Search logs for a GitLab CI pipeline.

    Returns matching log lines with job context.
    """
    args = {
        "project_id": project_id,
        "pipeline_id": pipeline_id,
        "pattern": query or ".*",  # Default to match all if no query
    }
    return await handle_gitlab_ci_search_logs(args, user)


@router.get("/gitlab/{project_id}/jobs/{job_id}/logs")
async def gitlab_get_job_logs(
    project_id: str = Path(..., description="GitLab project ID or path"),
    job_id: int = Path(..., description="Job ID"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Get complete logs for a specific GitLab CI job.

    Returns the full log output for the specified job.
    """
    args = {
        "project_id": project_id,
        "job_id": job_id,
    }
    return await handle_gitlab_ci_get_job_logs(args, user)


@router.post("/gitlab/{project_id}/pipelines/{pipeline_id}/retry")
async def gitlab_retry_pipeline(
    project_id: str = Path(..., description="GitLab project ID or path"),
    pipeline_id: int = Path(..., description="Pipeline ID to retry"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Retry a failed GitLab CI pipeline.

    Triggers a re-run of the specified pipeline.
    """
    args = {
        "project_id": project_id,
        "pipeline_id": pipeline_id,
    }
    return await handle_gitlab_ci_retry_pipeline(args, user)


@router.post("/gitlab/{project_id}/pipelines/{pipeline_id}/cancel")
async def gitlab_cancel_pipeline(
    project_id: str = Path(..., description="GitLab project ID or path"),
    pipeline_id: int = Path(..., description="Pipeline ID to cancel"),
    user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Cancel a running or pending GitLab CI pipeline.

    Stops the specified pipeline if it is in progress or pending.
    """
    args = {
        "project_id": project_id,
        "pipeline_id": pipeline_id,
    }
    return await handle_gitlab_ci_cancel_pipeline(args, user)
