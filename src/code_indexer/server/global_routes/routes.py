"""
REST API routes for global repository operations.

Provides REST endpoints for:
- Listing global repos
- Getting repo status
- Getting/setting global configuration

Uses GlobalRepoOperations for shared business logic with CLI and MCP.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field, field_validator
from typing import Dict, List, Any, Optional, cast

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.dependencies import (
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import User
from code_indexer.server.routers import repo_access_http
from code_indexer.global_repos.shared_operations import GlobalRepoOperations


# Router with /global prefix
router = APIRouter(prefix="/global", tags=["global"])


# Module-level golden repos directory (configurable for testing via set_golden_repos_dir)
_golden_repos_dir: Optional[str] = None


def set_golden_repos_dir(dir_path: str) -> None:
    """
    Set golden repos directory (for testing).

    Args:
        dir_path: Path to golden repos directory
    """
    global _golden_repos_dir
    _golden_repos_dir = dir_path


def _get_golden_repos_dir() -> str:
    """Get golden_repos_dir from app.state or test override.

    Raises:
        RuntimeError: If golden_repos_dir is not configured
    """
    from typing import cast

    # Check test override first
    if _golden_repos_dir:
        return _golden_repos_dir

    # Lazy import to avoid circular dependency (app.py imports this module)
    from code_indexer.server import app as app_module

    # Get from app.state
    golden_repos_dir: Optional[str] = cast(
        Optional[str], getattr(app_module.app.state, "golden_repos_dir", None)
    )
    if golden_repos_dir:
        return golden_repos_dir

    raise RuntimeError(
        "golden_repos_dir not configured. "
        "Server must set app.state.golden_repos_dir during startup, "
        "or use set_golden_repos_dir() for testing."
    )


def get_global_repo_operations() -> GlobalRepoOperations:
    """
    Get GlobalRepoOperations instance.

    Returns:
        GlobalRepoOperations configured with golden repos directory

    Raises:
        RuntimeError: If golden_repos_dir is not configured
    """
    return GlobalRepoOperations(_get_golden_repos_dir())


# Request/response models
class GlobalConfigUpdate(BaseModel):
    """Request model for updating global configuration."""

    refresh_interval: int = Field(
        ..., ge=60, description="Refresh interval in seconds (minimum 60)"
    )

    @field_validator("refresh_interval")
    @classmethod
    def validate_minimum(cls, v: int) -> int:
        """Validate refresh interval is at least 60 seconds."""
        if v < 60:
            raise ValueError("Refresh interval must be at least 60 seconds")
        return v


class GlobalReposResponse(BaseModel):
    """Response model for list global repos."""

    repos: List[Dict[str, Any]]


class ConfigResponse(BaseModel):
    """Response model for get config."""

    refresh_interval: int


class ConfigUpdateResponse(BaseModel):
    """Response model for config update."""

    status: str


# Endpoints
@router.get("/repos", response_model=GlobalReposResponse)
def list_global_repos(
    request: Request,
    user: User = Depends(get_current_user),
) -> GlobalReposResponse:
    """
    List the global repositories the caller has group access to.

    Admins see every repository (same listing as the MCP list_global_repos
    tool, filtered by repo_name). Fails closed with 500 when the access
    service is unavailable.

    Requires authentication.

    Returns:
        GlobalReposResponse with list of repository metadata
    """
    access_filtering_service = getattr(
        request.app.state, "access_filtering_service", None
    )
    repo_access_http.enforce_repo_access(access_filtering_service, user.username, None)
    ops = get_global_repo_operations()
    repos = ops.list_repos()
    accessible = set(
        repo_access_http.accessible_repo_names(
            access_filtering_service,
            user.username,
            [r.get("repo_name", "") for r in repos],
        )
    )

    return GlobalReposResponse(
        repos=[r for r in repos if r.get("repo_name", "") in accessible]
    )


@router.get("/repos/{alias}/status")
def get_repo_status(
    alias: str, request: Request, user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Get status of a specific global repository.

    Args:
        alias: Global repository alias name

    Requires authentication.

    Returns:
        Repository status dict with metadata

    Raises:
        HTTPException 403: caller lacks group access to the repository.
        HTTPException 404: If repository not found
        HTTPException 500: access service unavailable (fails closed).
    """
    # Reading a global repository's status requires group access to it,
    # checked before its existence is looked up.
    repo_access_http.enforce_repo_access(
        getattr(request.app.state, "access_filtering_service", None),
        user.username,
        alias,
    )
    ops = get_global_repo_operations()

    try:
        status = ops.get_status(alias)
        return cast(Dict[str, Any], status)
    except ValueError as e:
        # Map ValueError (repo not found) to HTTP 404
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/config", response_model=ConfigResponse)
def get_global_config(user: User = Depends(get_current_user)) -> ConfigResponse:
    """
    Get global configuration.

    Returns current global refresh configuration.

    Requires authentication.

    Returns:
        ConfigResponse with refresh_interval
    """
    ops = get_global_repo_operations()
    config = ops.get_config()

    return ConfigResponse(refresh_interval=config["refresh_interval"])


@router.put(
    "/config",
    response_model=ConfigUpdateResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def update_global_config(
    config: GlobalConfigUpdate,
    user: User = Depends(get_current_admin_user_hybrid),
) -> ConfigUpdateResponse:
    """
    Update global configuration.

    Updates the global refresh interval.

    This mutates server-wide config (the golden-repo refresh
    interval used across the whole fleet), so it requires admin role AND an
    active TOTP elevation window -- matching the MCP twin `set_global_config`
    (`@require_mcp_elevation()` in `mcp/handlers/admin/__init__.py`).

    Args:
        config: GlobalConfigUpdate with new refresh_interval

    Requires admin role + elevation.

    Returns:
        ConfigUpdateResponse with status="updated"

    Raises:
        HTTPException 400: If refresh_interval < 60 seconds
    """
    ops = get_global_repo_operations()

    try:
        ops.set_config(config.refresh_interval, actor=user.username)
        return ConfigUpdateResponse(status="updated")
    except ValueError as e:
        # Map ValueError (validation failed) to HTTP 400
        raise HTTPException(status_code=400, detail=str(e))
