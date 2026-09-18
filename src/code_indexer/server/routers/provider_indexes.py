"""REST endpoints for provider-specific index management (Story #490)."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from ..auth import dependencies
from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User

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
    from code_indexer.server.mcp.handlers import (
        _resolve_golden_repo_path,
        _resolve_golden_repo_base_clone,
        _remove_provider_from_config,
    )

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    error = service.validate_provider(body.provider)
    if error:
        raise HTTPException(status_code=400, detail=error)

    repo_path = _resolve_golden_repo_path(body.alias)
    if not repo_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository '{body.alias}' not found",
        )

    # Bug #625 W6: Write operations require the mutable base clone path.
    base_clone = _resolve_golden_repo_base_clone(body.alias)
    if not base_clone:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cannot resolve base clone for '{body.alias}'. "
            "Remove requires a writable base clone path.",
        )
    # P2 fix: _remove_provider_from_config performs a synchronous
    # fsync+chmod+os.replace write (write_json_atomic). Offload it to a
    # worker thread so a slow/hard NFS mount never blocks the whole
    # event loop (900-repo production scale).
    await anyio.to_thread.run_sync(
        _remove_provider_from_config, base_clone, body.provider
    )
    result = service.remove_provider_index(base_clone, body.provider)
    return {
        "success": result["removed"],
        "collection_name": result["collection_name"],
        "message": result["message"],
    }


def _prepare_bulk_add_jobs(
    global_repos: List[Dict[str, Any]],
    filter_str: Optional[str],
    provider: str,
    service: Any,
) -> "tuple[List[Dict[str, str]], List[str]]":
    """Synchronous per-repo batch work for bulk_add(): resolve repo paths,
    check existing provider status, and write the provider into each
    repo's base-clone config.json (_append_provider_to_config -- fsync +
    chmod + os.replace via write_json_atomic).

    Extracted so the WHOLE batch (up to ~900 repos) can be offloaded via a
    single `anyio.to_thread.run_sync` call from bulk_add(), instead of one
    blocking write per repo directly on the event loop.

    Returns (to_submit, skipped) where to_submit holds {"alias", "repo_path"}
    dicts for repos whose config write succeeded and a background job still
    needs to be submitted.
    """
    from code_indexer.server.mcp.handlers import (
        _resolve_golden_repo_path,
        _resolve_golden_repo_base_clone,
        _append_provider_to_config,
    )

    to_submit: List[Dict[str, str]] = []
    skipped: List[str] = []

    for repo in global_repos:
        alias = repo.get("alias_name", "")

        if filter_str:
            category = repo.get("category", "")
            if filter_str.startswith("category:"):
                filter_cat = filter_str.split(":", 1)[1]
                if filter_cat.lower() not in category.lower():
                    continue

        repo_path = _resolve_golden_repo_path(alias)
        if not repo_path:
            continue

        repo_status = service.get_provider_index_status(repo_path, alias)
        if repo_status.get(provider, {}).get("exists"):
            skipped.append(alias)
            continue

        # Bug #625 W3: Write provider to base clone config before submitting job
        base_clone = _resolve_golden_repo_base_clone(alias)
        if not base_clone:
            skipped.append(alias)
            continue
        if not _append_provider_to_config(base_clone, provider):
            skipped.append(alias)
            continue

        to_submit.append({"alias": alias, "repo_path": repo_path})

    return to_submit, skipped


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
    from code_indexer.server.mcp.handlers import (
        _list_global_repos,
        _provider_index_job,
    )

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    error = service.validate_provider(body.provider)
    if error:
        raise HTTPException(status_code=400, detail=error)

    global_repos = _list_global_repos()
    app = request.app

    # P2 fix: offload the ENTIRE synchronous batch (path resolution +
    # status check + config write for every repo) in ONE worker-thread
    # call, not one anyio.to_thread.run_sync per repo -- the latter just
    # serializes N thread hops back onto the event loop and still blocks
    # it between hops at 900-repo scale.
    to_submit, skipped = await anyio.to_thread.run_sync(
        _prepare_bulk_add_jobs, global_repos, body.filter, body.provider, service
    )

    job_ids: List[Dict[str, str]] = []
    for item in to_submit:
        alias = item["alias"]
        repo_path = item["repo_path"]
        job_id = app.state.background_job_manager.submit_job(
            operation_type="provider_index_add",
            func=_provider_index_job,
            submitter_username=current_user.username,
            repo_alias=alias,
            repo_path=repo_path,
            provider_name=body.provider,
            clear=False,
            # Pod-pull: reconstruction params for _provider_index_job.
            metadata={
                "repo_path": repo_path,
                "provider_name": body.provider,
                "clear": False,
            },
        )
        job_ids.append({"alias": alias, "job_id": str(job_id)})

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
    """Submit a provider index job."""
    import anyio.to_thread

    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.mcp.handlers import (
        _resolve_golden_repo_path,
        _resolve_golden_repo_base_clone,
        _append_provider_to_config,
        _provider_index_job,
    )

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    error = service.validate_provider(provider)
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    repo_path = _resolve_golden_repo_path(alias)
    if not repo_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository '{alias}' not found",
        )

    # Bug #625 W4: Write provider to config on base clone before submitting job
    if not clear:  # "add" action — write provider to config
        base_clone = _resolve_golden_repo_base_clone(alias)
        if not base_clone:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Cannot resolve base clone for '{alias}'. "
                "Add requires a writable base clone path.",
            )
        # P2 fix: _append_provider_to_config performs a synchronous
        # fsync+chmod+os.replace write (write_json_atomic). Offload it to
        # a worker thread so a slow/hard NFS mount never blocks the whole
        # event loop (900-repo production scale) -- same defect class
        # already fixed in remove_provider_index()/bulk_add().
        wrote_ok = await anyio.to_thread.run_sync(
            _append_provider_to_config, base_clone, provider
        )
        if not wrote_ok:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to write provider '{provider}' to config at {base_clone}",
            )

    action = "recreate" if clear else "add"
    app = request.app

    job_id = app.state.background_job_manager.submit_job(
        operation_type=f"provider_index_{action}",
        func=_provider_index_job,
        submitter_username=current_user.username,
        repo_alias=alias,
        repo_path=repo_path,
        provider_name=provider,
        clear=clear,
    )

    return {
        "success": True,
        "job_id": str(job_id),
        "message": f"Background job submitted to {action} {provider} index for {alias}",
    }
