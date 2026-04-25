"""REST endpoint for provider health status (Bug #679 Part 2 AC3)."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth.dependencies import get_current_admin_user_hybrid
from ..auth.user_manager import User
from ...services.provider_health_monitor import ProviderHealthMonitor

router = APIRouter(prefix="/admin/provider-health", tags=["admin-provider-health"])


class ProviderHealthEntry(BaseModel):
    provider: str
    status: str
    sinbinned: bool
    sinbin_expires_at: Optional[float]
    sinbin_rounds: int
    p50_latency_ms: float
    p95_latency_ms: float
    error_rate: float
    total_requests: int
    successful_requests: int
    failed_requests: int
    window_minutes: int


class ProviderHealthResponse(BaseModel):
    providers: List[ProviderHealthEntry]


class ClearSinbinRequest(BaseModel):
    """Optional body for POST /admin/provider-health/clear-sinbin (Bug #902).

    target: provider name to clear (e.g. 'voyage-ai', 'cohere').
            Omit or pass null to clear ALL providers at once.
    """

    target: Optional[str] = None


@router.post("/clear-sinbin")
def clear_sinbin(
    body: ClearSinbinRequest,
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Force-clear ProviderHealthMonitor sinbin state (Bug #902).

    Clears the named provider when 'target' is supplied, otherwise clears ALL
    providers.  Designed for use by the Phase 5 E2E test fixture clear_all_faults
    to break the chicken-and-egg where sinbinned providers are skipped by dispatch
    so they can never self-heal via record_call(success=True).
    """
    monitor = ProviderHealthMonitor.get_instance()
    if body.target is not None:
        monitor.clear_sinbin(body.target)
        return {"cleared": body.target}
    monitor.clear_sinbin_all()
    return {"cleared": "all"}


@router.get("", response_model=ProviderHealthResponse)
def get_provider_health(
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    """Return health status for all tracked embedding/reranking providers.

    sinbin_expires_at is the remaining TTL in seconds until the sin-bin
    cooldown expires, or null when the provider is not sinbinned.
    """
    monitor = ProviderHealthMonitor.get_instance()
    health_map = monitor.get_health()

    entries: List[Dict[str, Any]] = []
    for provider_name, health_status in health_map.items():
        is_sinbinned = monitor.is_sinbinned(provider_name)
        sinbin_expires_at: Optional[float] = (
            monitor.get_sinbin_ttl_seconds(provider_name) if is_sinbinned else None
        )
        sinbin_rounds = monitor.get_sinbin_rounds(provider_name)

        entries.append(
            {
                "provider": provider_name,
                "status": health_status.status,
                "sinbinned": is_sinbinned,
                "sinbin_expires_at": sinbin_expires_at,
                "sinbin_rounds": sinbin_rounds,
                "p50_latency_ms": health_status.p50_latency_ms,
                "p95_latency_ms": health_status.p95_latency_ms,
                "error_rate": health_status.error_rate,
                "total_requests": health_status.total_requests,
                "successful_requests": health_status.successful_requests,
                "failed_requests": health_status.failed_requests,
                "window_minutes": health_status.window_minutes,
            }
        )

    return {"providers": entries}
