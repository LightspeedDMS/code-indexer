"""SIEM delivery admin REST surface (admin role; actions need TOTP elevation).

Every route is a sync ``def`` (FastAPI runs it on the threadpool), so no
database or network I/O ever runs on the event loop.  ``local_process`` in
the stats response is THIS process only; everything else is fleet state.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ..auth.dependencies import get_current_admin_user_hybrid, require_elevation
from ..auth.user_manager import User

router = APIRouter(prefix="/api/admin/siem-delivery", tags=["siem-delivery-admin"])

QUARANTINE_LISTING_MAX = 1000


def _scheduler(request: Request) -> Any:
    from ..services.siem_delivery.admin import find_scheduler

    scheduler, error = find_scheduler(request.app.state)
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail=f"SIEM delivery is not running in this process: {error}",
        )
    return scheduler


def _run(fn: Any, *args: Any) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import SiemAdminError

    try:
        result: Dict[str, Any] = fn(*args)
        return result
    except SiemAdminError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


@router.get("/stats")
def get_siem_delivery_stats(
    request: Request, current_user: User = Depends(get_current_admin_user_hybrid)
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import stats_document

    return stats_document(_scheduler(request))


@router.get("/quarantine")
def list_siem_quarantine(
    request: Request,
    limit: int = Query(100, ge=1, le=QUARANTINE_LISTING_MAX),
    current_user: User = Depends(get_current_admin_user_hybrid),
) -> Dict[str, Any]:
    from ..services.siem_delivery.stats import list_quarantined

    return {"rows": list_quarantined(_scheduler(request).db, limit)}


@router.post("/canary")
def run_siem_canary(
    request: Request, current_user: User = Depends(require_elevation())
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import run_canary

    return _run(run_canary, _scheduler(request), current_user.username)


@router.post("/canary/confirm-visible")
def confirm_siem_canary_visible(
    request: Request,
    canary_run_id: str = Body(..., embed=True),
    visible_product_log_ids: List[str] = Body(..., embed=True),
    current_user: User = Depends(require_elevation()),
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import confirm_visible

    return _run(
        confirm_visible,
        _scheduler(request),
        current_user.username,
        canary_run_id,
        visible_product_log_ids,
    )


@router.post("/resume")
def resume_siem_delivery(
    request: Request, current_user: User = Depends(require_elevation())
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import resume

    return _run(resume, _scheduler(request), current_user.username)


@router.post("/quarantine/requeue")
def requeue_siem_quarantine(
    request: Request,
    event_uuids: List[str] = Body(..., embed=True),
    current_user: User = Depends(require_elevation()),
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import requeue_quarantined

    return _run(
        requeue_quarantined, _scheduler(request), current_user.username, event_uuids
    )


@router.post("/batches/{batch_id}/acknowledge")
def acknowledge_siem_batch(
    batch_id: str, request: Request, current_user: User = Depends(require_elevation())
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import acknowledge_batch

    return _run(acknowledge_batch, _scheduler(request), current_user.username, batch_id)


@router.post("/batches/{batch_id}/rebatch")
def rebatch_siem_batch(
    batch_id: str, request: Request, current_user: User = Depends(require_elevation())
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import rebatch_batch

    return _run(rebatch_batch, _scheduler(request), current_user.username, batch_id)


@router.post("/destinations/{destination_key}/retarget")
def retarget_siem_destination(
    destination_key: str,
    request: Request,
    current_user: User = Depends(require_elevation()),
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import retarget_destination

    return _run(
        retarget_destination,
        _scheduler(request),
        current_user.username,
        destination_key,
    )


@router.post("/destinations/{destination_key}/abandon")
def abandon_siem_destination(
    destination_key: str,
    request: Request,
    current_user: User = Depends(require_elevation()),
) -> Dict[str, Any]:
    from ..services.siem_delivery.admin import abandon_destination

    return _run(
        abandon_destination, _scheduler(request), current_user.username, destination_key
    )
