"""Maintenance Mode Router for CIDX Server.

Story #734: Job-Aware Auto-Update with Graceful Drain Mode
Bug #135: Dynamic drain timeout calculation from server config

Provides admin API endpoints for maintenance mode management.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from code_indexer.server.auth.dependencies import get_current_admin_user
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.maintenance_service import get_maintenance_state
from code_indexer.server.utils.config_manager import ServerConfigManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/maintenance", tags=["maintenance"])


def get_config_manager() -> ServerConfigManager:
    """Get ServerConfigManager instance (dependency injection point for testing)."""
    return ServerConfigManager()


@router.post("/enter")
def enter_maintenance_mode(
    current_user: User = Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """Enter maintenance mode.

    Stops accepting new jobs while allowing running jobs to complete.
    Query endpoints remain available during maintenance.

    Returns:
        Dict with maintenance_mode, running_jobs, queued_jobs, message
    """
    state = get_maintenance_state()
    result = state.enter_maintenance_mode()
    logger.info(f"Maintenance mode entered: {result}")
    return result


@router.post("/exit")
def exit_maintenance_mode(
    current_user: User = Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """Exit maintenance mode.

    Resumes accepting new jobs.

    Returns:
        Dict with maintenance_mode, message
    """
    state = get_maintenance_state()
    result = state.exit_maintenance_mode()
    logger.info(f"Maintenance mode exited: {result}")
    return result


@router.get("/status")
def get_maintenance_status(
    current_user: User = Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """Get current maintenance mode status.

    Returns:
        Dict with maintenance_mode, drained, running_jobs, queued_jobs, entered_at
    """
    state = get_maintenance_state()
    return state.get_status()


@router.get("/drain-status")
def get_drain_status(
    current_user: User = Depends(get_current_admin_user),
) -> Dict[str, Any]:
    """Get drain status for auto-update coordination (AC2).

    Returns drained: true when running_jobs == 0 and queued_jobs == 0.
    Includes job details for monitoring purposes.

    Returns:
        Dict with drained, running_jobs, queued_jobs, estimated_drain_seconds, jobs
    """
    state = get_maintenance_state()
    return state.get_drain_status()


@router.get("/drain-timeout")
def get_drain_timeout(
    current_user: User = Depends(get_current_admin_user),
    config_manager: ServerConfigManager = Depends(get_config_manager),
) -> Dict[str, Any]:
    """Get recommended drain timeout based on server configuration (Bug #135).

    Calculates drain timeout dynamically from configured job timeouts instead of
    using hardcoded values. DeploymentExecutor queries this endpoint to determine
    how long to wait for jobs to drain before forcing server restart.

    Returns:
        Dict with max_job_timeout_seconds and recommended_drain_timeout_seconds
    """
    # Load current configuration
    config = config_manager.load_config()
    if config is None:
        # Fallback to default config if none exists
        config = config_manager.create_default_config()

    # Calculate timeouts from config
    state = get_maintenance_state()
    max_timeout = state.get_max_job_timeout(config)
    recommended_timeout = state.get_recommended_drain_timeout(config)

    return {
        "max_job_timeout_seconds": max_timeout,
        "recommended_drain_timeout_seconds": recommended_timeout,
    }
