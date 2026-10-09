"""REST endpoints for provider-specific index management (Story #490)."""

import functools
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..auth import dependencies
from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User
from ..services import golden_repo_audited_ops as ops

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/provider-indexes", tags=["provider-indexes"])


class ProviderInfoItem(BaseModel):
    name: str
    display_name: str
    default_model: str
    supports_batch: bool
    api_key_env: str


class ProviderIndexRequest(BaseModel):
    provider: str
    alias: str


class BulkAddRequest(BaseModel):
    provider: str
    filter: Optional[str] = None


@router.get("/providers")
async def list_providers(
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """List configured embedding providers with valid API keys."""
    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)
    providers = service.list_providers()

    return {"providers": providers, "count": len(providers)}


@router.get("/status")
async def get_provider_index_status(
    alias: str,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Get per-provider index status for a repository."""
    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.mcp.handlers import _resolve_golden_repo_path

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    repo_path = _resolve_golden_repo_path(alias)
    if not repo_path:
        raise HTTPException(status_code=404, detail=f"Repository '{alias}' not found")

    status = service.get_provider_index_status(repo_path, alias)
    return {"repository_alias": alias, "provider_indexes": status}


@router.post(
    "/add", status_code=202, dependencies=[Depends(dependencies.require_elevation())]
)
async def add_provider_index(
    body: ProviderIndexRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Add provider index for a repository (background job)."""
    return await _submit_index_job(
        body.provider,
        body.alias,
        clear=False,
        request=request,
        current_user=current_user,
    )


@router.post(
    "/recreate",
    status_code=202,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def recreate_provider_index(
    body: ProviderIndexRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Recreate provider index from scratch (background job)."""
    return await _submit_index_job(
        body.provider,
        body.alias,
        clear=True,
        request=request,
        current_user=current_user,
    )


@router.post("/remove", dependencies=[Depends(dependencies.require_elevation())])
async def remove_provider_index(
    body: ProviderIndexRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Remove a provider's collection from a repository."""
    import anyio.to_thread

    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    # The whole removal (config write, collection delete and its audit row)
    # runs in one worker-thread call: a slow/hard NFS mount never blocks
    # the event loop (900-repo production scale).
    try:
        result = await anyio.to_thread.run_sync(
            functools.partial(
                ops.remove_provider_index_audited,
                service=service,
                provider=body.provider,
                alias=body.alias,
                actor=current_user.username,
            )
        )
    except ops.ProviderIndexRequestError as error:
        raise _provider_http_error(error, body.alias, body.provider, "Remove")
    return {
        "success": result["removed"],
        "collection_name": result["collection_name"],
        "message": result["message"],
    }


def _provider_http_error(
    error: "ops.ProviderIndexRequestError", alias: str, provider: str, verb: str
) -> HTTPException:
    """This door's HTTP error for a refused provider-index request."""
    if error.kind in (ops.INVALID_PROVIDER, ops.INVALID_FILTER):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=error.detail
        )
    if error.kind == ops.CATEGORIES_UNAVAILABLE:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Repository category service not available; "
            "the category filter cannot be applied",
        )
    if error.kind == ops.REPO_NOT_FOUND:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository '{alias}' not found",
        )
    if error.kind == ops.CONFIG_WRITE_FAILED:
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write provider '{provider}' to config at {error.detail}",
        )
    if error.kind == ops.JOB_MANAGER_UNAVAILABLE:
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Background job manager not available",
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"Cannot resolve base clone for '{alias}'. "
        f"{verb} requires a writable base clone path.",
    )


@router.post(
    "/bulk-add",
    status_code=202,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def bulk_add(
    body: BulkAddRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Bulk add provider index to all repositories that lack it."""
    import anyio.to_thread

    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    # The ENTIRE batch (path resolution, status check and config write for
    # every repo, the job submissions, and the one audit row) runs in ONE
    # worker-thread call, never one hop per repo (900-repo scale).
    try:
        job_ids, skipped = await anyio.to_thread.run_sync(
            functools.partial(
                ops.bulk_add_provider_index_audited,
                job_manager=request.app.state.background_job_manager,
                service=service,
                provider=body.provider,
                filter_str=body.filter,
                actor=current_user.username,
            )
        )
    except ops.ProviderIndexRequestError as error:
        raise _provider_http_error(error, "", body.provider, "Add")

    return {
        "success": True,
        "provider": body.provider,
        "jobs_created": len(job_ids),
        "jobs": job_ids,
        "skipped": skipped,
        "skipped_count": len(skipped),
        "message": f"Created {len(job_ids)} jobs, skipped {len(skipped)} repos",
    }


@router.get("/health")
async def get_provider_health_rest(
    provider: Optional[str] = None,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Get provider health metrics."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    monitor = ProviderHealthMonitor.get_instance()
    health = monitor.get_health(provider)

    result = {}
    for pname, health_status in health.items():
        result[pname] = {
            "status": health_status.status,
            "health_score": health_status.health_score,
            "p50_latency_ms": health_status.p50_latency_ms,
            "p95_latency_ms": health_status.p95_latency_ms,
            "p99_latency_ms": health_status.p99_latency_ms,
            "error_rate": health_status.error_rate,
            "availability": health_status.availability,
            "total_requests": health_status.total_requests,
        }

    return {"provider_health": result}


async def _submit_index_job(
    provider: str, alias: str, clear: bool, request: Request, current_user: User
) -> Dict[str, Any]:
    """Submit a provider index job.

    The shared provider-index entry point (also used by MCP) writes the
    provider into the base clone's config, submits the job and records the
    audit row; the whole operation runs in one worker-thread call so a
    slow/hard NFS mount never blocks the event loop (900-repo scale).
    """
    import anyio.to_thread

    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)
    action = "recreate" if clear else "add"

    try:
        job_id = await anyio.to_thread.run_sync(
            functools.partial(
                ops.submit_provider_index_job,
                job_manager=request.app.state.background_job_manager,
                service=service,
                action=action,
                provider=provider,
                alias=alias,
                actor=current_user.username,
            )
        )
    except ops.ProviderIndexRequestError as error:
        raise _provider_http_error(error, alias, provider, action.capitalize())

    return {
        "success": True,
        "job_id": str(job_id),
        "message": f"Background job submitted to {action} {provider} index for {alias}",
    }
