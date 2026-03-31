"""REST endpoints for provider-specific index management (Story #490)."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

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


@router.post("/add", status_code=202)
async def add_provider_index(
    body: ProviderIndexRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Add provider index for a repository (background job)."""
    return _submit_index_job(
        body.provider,
        body.alias,
        clear=False,
        request=request,
        current_user=current_user,
    )


@router.post("/recreate", status_code=202)
async def recreate_provider_index(
    body: ProviderIndexRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Recreate provider index from scratch (background job)."""
    return _submit_index_job(
        body.provider,
        body.alias,
        clear=True,
        request=request,
        current_user=current_user,
    )


@router.post("/remove")
async def remove_provider_index(
    body: ProviderIndexRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Remove a provider's collection from a repository."""
    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.mcp.handlers import _resolve_golden_repo_path

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    error = service.validate_provider(body.provider)
    if error:
        raise HTTPException(status_code=400, detail=error)

    repo_path = _resolve_golden_repo_path(body.alias)
    if not repo_path:
        raise HTTPException(
            status_code=404, detail=f"Repository '{body.alias}' not found"
        )

    result = service.remove_provider_index(repo_path, body.provider)
    return {
        "success": result["removed"],
        "collection_name": result["collection_name"],
        "message": result["message"],
    }


@router.post("/bulk-add", status_code=202)
async def bulk_add(
    body: BulkAddRequest,
    request: Request,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Bulk add provider index to all repositories that lack it."""
    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.mcp.handlers import (
        _resolve_golden_repo_path,
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

    job_ids: List[Dict[str, str]] = []
    skipped: List[str] = []

    for repo in global_repos:
        alias = repo.get("alias_name", "")

        if body.filter:
            category = repo.get("category", "")
            if body.filter.startswith("category:"):
                filter_cat = body.filter.split(":", 1)[1]
                if filter_cat.lower() not in category.lower():
                    continue

        repo_path = _resolve_golden_repo_path(alias)
        if not repo_path:
            continue

        status = service.get_provider_index_status(repo_path, alias)
        if status.get(body.provider, {}).get("exists"):
            skipped.append(alias)
            continue

        job_id = app.state.background_job_manager.submit_job(
            operation_type="provider_index_add",
            func=_provider_index_job,
            submitter_username=current_user.username,
            repo_alias=alias,
            repo_path=repo_path,
            provider_name=body.provider,
            clear=False,
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


def _submit_index_job(
    provider: str, alias: str, clear: bool, request: Request, current_user: User
) -> Dict[str, Any]:
    """Submit a provider index job."""
    from code_indexer.server.services.provider_index_service import ProviderIndexService
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.mcp.handlers import (
        _resolve_golden_repo_path,
        _provider_index_job,
    )

    config = get_config_service().get_config()
    service = ProviderIndexService(config=config)

    error = service.validate_provider(provider)
    if error:
        raise HTTPException(status_code=400, detail=error)

    repo_path = _resolve_golden_repo_path(alias)
    if not repo_path:
        raise HTTPException(status_code=404, detail=f"Repository '{alias}' not found")

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
