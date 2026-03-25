"""
Miscellaneous and system route handlers extracted from inline_routes.py.

Part of the inline_routes.py modularization effort. Contains 6 route handlers:
- GET /health
- GET /cache/stats
- GET /api/system/health
- GET /.well-known/oauth-authorization-server
- GET /.well-known/oauth-protected-resource
- GET /favicon.ico

Zero behavior change: same paths, methods, response models, and handler logic.
"""

import logging

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    status,
    Depends,
)

from ..models.api_models import HealthCheckResponse
from ..auth import dependencies
from ..logging_utils import format_error_log
from ..middleware.correlation import get_correlation_id
from ..services.health_service import health_service
from ..services.maintenance_service import get_maintenance_state
from ..app_helpers import (
    get_server_uptime,
    get_server_start_time,
    get_system_resources,
    check_database_health,
    get_recent_errors,
)

# Module-level logger
logger = logging.getLogger(__name__)


def register_misc_routes(
    app: FastAPI,
    *,
    golden_repo_manager,
    activated_repo_manager,
    config_service,
    server_config,
    data_dir: str,
    background_job_manager,
    user_manager,
) -> None:
    """
    Register miscellaneous and system route handlers onto the FastAPI app.

    Each handler is defined as a closure over the function parameters,
    exactly as they were closures over create_app() locals before extraction.
    No handler logic is changed.

    Args:
        app: The FastAPI application instance
        golden_repo_manager: GoldenRepoManager instance
        activated_repo_manager: ActivatedRepoManager instance
        config_service: ConfigService instance
        server_config: ServerConfig instance
        data_dir: Server data directory path
        background_job_manager: BackgroundJobManager instance
        user_manager: UserManager instance
    """

    # Health endpoint (requires authentication for security)
    @app.get("/health")
    def health_check(
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Enhanced health check endpoint.

        Provides detailed server status including uptime, job queue health,
        system resource usage, and recent error information. Authentication
        required to prevent information disclosure.
        """
        try:
            # Calculate uptime
            uptime = get_server_uptime()

            # Get job queue health
            try:
                active_jobs = (
                    background_job_manager.get_active_job_count()
                    if background_job_manager
                    else 0
                )
            except Exception:
                active_jobs = 0

            try:
                pending_jobs = (
                    background_job_manager.get_pending_job_count()
                    if background_job_manager
                    else 0
                )
            except Exception:
                pending_jobs = 0

            try:
                failed_jobs = (
                    background_job_manager.get_failed_job_count()
                    if background_job_manager
                    else 0
                )
            except Exception:
                failed_jobs = 0

            # Get system resources
            try:
                system_resources = get_system_resources()
            except Exception:
                system_resources = None

            # Get database health
            try:
                database_health = check_database_health(
                    user_manager=user_manager,
                    background_job_manager=background_job_manager,
                )
            except Exception:
                database_health = None

            # Get recent errors
            try:
                recent_errors = get_recent_errors()
            except Exception:
                recent_errors = None

            # Determine overall status
            health_status = "healthy"
            message = "CIDX Server is running"

            if failed_jobs > 0:
                health_status = "degraded"
                message = (
                    f"CIDX Server is running but {failed_jobs} failed jobs detected"
                )
            elif pending_jobs > 8:  # High pending job threshold
                health_status = "warning"
                message = f"CIDX Server is running with high pending job count ({pending_jobs})"

            health_response = {
                "status": health_status,
                "message": message,
                "uptime": uptime,
                "active_jobs": active_jobs,
                "job_queue": {
                    "active_jobs": active_jobs,
                    "pending_jobs": pending_jobs,
                    "failed_jobs": failed_jobs,
                },
                "started_at": get_server_start_time(),
                "maintenance_mode": get_maintenance_state().is_maintenance_mode(),
            }

            # Add version if available
            try:
                from code_indexer import __version__

                health_response["version"] = __version__
            except Exception:
                health_response["version"] = "unknown"

            # Add system resources if available
            if system_resources:
                health_response["system_resources"] = system_resources

            # Add database health if available
            if database_health:
                health_response["database"] = database_health  # type: ignore[assignment]

            # Add recent errors if available
            if recent_errors:
                health_response["recent_errors"] = recent_errors  # type: ignore[assignment]

            return health_response

        except Exception as e:
            # Health endpoint should never fail completely
            return {
                "status": "degraded",
                "message": f"Health check partial failure: {str(e)}",
                "uptime": None,
                "active_jobs": 0,
            }

    # Cache statistics endpoint (Story #526: HNSW Index Cache monitoring)
    @app.get("/cache/stats")
    def get_cache_stats(
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get HNSW index cache statistics.

        Story #526: Server-Side HNSW Index Caching for 1800x Query Performance

        Returns cache performance metrics including:
        - Total cached repositories
        - Cache hit/miss ratios
        - Memory usage estimates
        - Per-repository access statistics
        - TTL remaining for each cached entry

        Requires authentication for security.

        Returns:
            JSON with cache statistics:
            {
                "cached_repositories": int,
                "total_memory_mb": float,
                "hit_count": int,
                "miss_count": int,
                "hit_ratio": float,
                "eviction_count": int,
                "per_repository_stats": {
                    "repo_path": {
                        "access_count": int,
                        "last_accessed": str (ISO datetime),
                        "created_at": str (ISO datetime),
                        "ttl_remaining_seconds": float
                    }
                }
            }
        """
        try:
            # Import cache singleton
            from code_indexer.server.cache import get_global_cache

            # Get cache instance
            cache = get_global_cache()

            # Get statistics
            stats = cache.get_stats()

            # Convert to JSON-serializable dictionary
            return {
                "cached_repositories": stats.cached_repositories,
                "total_memory_mb": stats.total_memory_mb,
                "hit_count": stats.hit_count,
                "miss_count": stats.miss_count,
                "hit_ratio": stats.hit_ratio,
                "eviction_count": stats.eviction_count,
                "per_repository_stats": stats.per_repository_stats,
            }

        except Exception as e:
            # Log error but don't expose internal details
            logger.error(
                format_error_log(
                    "APP-GENERAL-029",
                    f"Error retrieving cache statistics: {e}",
                    exc_info=True,
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve cache statistics",
            )

    # Health Check Endpoint
    @app.get("/api/system/health", response_model=HealthCheckResponse)
    def get_system_health(
        current_user: dependencies.User = Depends(dependencies.get_current_user),
    ):
        """
        Get comprehensive system health status.

        Monitors real system resources, database connectivity, and service health.
        Following CLAUDE.md Foundation #1: Uses real system checks, no mocks.

        SECURITY: Requires authentication to prevent information disclosure.
        """
        try:
            health_response = health_service.get_system_health()
            return health_response

        except Exception as e:
            logging.error(f"Health check failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Health check failed: {str(e)}",
            )

    # RFC 8414 compliance: OAuth discovery at root level for Claude.ai compatibility
    @app.get("/.well-known/oauth-authorization-server")
    def root_oauth_discovery(request: Request):
        """OAuth 2.1 discovery endpoint at root path (RFC 8414 compliance)."""
        oauth_manager = request.app.state.oauth_manager
        return oauth_manager.get_discovery_metadata()

    # RFC 9728 compliance: OAuth Protected Resource Metadata
    @app.get("/.well-known/oauth-protected-resource")
    def oauth_protected_resource_metadata():
        """OAuth 2.0 Protected Resource Metadata endpoint (RFC 9728 compliance)."""
        import os

        issuer_url = os.getenv("CIDX_ISSUER_URL", "http://localhost:8000")

        return {
            "resource": f"{issuer_url}/mcp",
            "authorization_servers": [issuer_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp:read", "mcp:write"],
            "resource_documentation": "https://github.com/LightspeedDMS/code-indexer",
        }

    # Favicon redirect
    @app.get("/favicon.ico")
    def favicon():
        """Redirect favicon.ico to admin static files."""
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/admin/static/favicon.svg", status_code=302)
