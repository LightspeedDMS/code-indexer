"""Web front door for the SIEM delivery operator actions (``/admin/siem-delivery``).

Admin only.  Reads (the arming and recovery panels, the destination dialog)
need an admin session and no elevation; every action additionally needs TOTP
elevation through the shared ``require_elevation()`` (which follows the
global enforcement switch) and a valid CSRF token.  Every action calls the
SAME service function as the REST router (``services/siem_delivery/admin``),
which writes the audit row; this module holds no business logic, no SQL and
no per-node state.

Reads are sync ``def`` (threadpool); actions are ``async`` only to read the
capped form, and run the service in a worker thread.
"""

from __future__ import annotations

import functools
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi import status as http_status
from fastapi.responses import Response
from starlette.datastructures import FormData

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.siem_delivery import admin, ops_documents
from code_indexer.server.services.siem_delivery.stats import COUNT_CAP

from . import siem_forms
from .routes import templates

siem_delivery_web_router = APIRouter(tags=["siem-delivery-web"])

RESULT_TEMPLATE = "partials/siem_delivery_ops_result.html"
ARMING_TEMPLATE = "partials/siem_delivery_ops_arming.html"
RECOVERY_TEMPLATE = "partials/siem_delivery_ops_recovery.html"
DIALOG_TEMPLATE = "partials/siem_delivery_ops_destination_dialog.html"
CHANGED_EVENT = "siem-ops-changed"  # refreshes both panel regions


def _fragment(
    request: Request,
    status: int,
    *,
    action: Optional[str] = None,
    message: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
) -> Response:
    """The one result fragment: the outcome, or the refusal verbatim."""
    response = templates.TemplateResponse(
        request,
        RESULT_TEMPLATE,
        {"status": status, "action": action, "message": message, "result": result},
        status_code=status,
    )
    if status == http_status.HTTP_200_OK:
        response.headers["HX-Trigger"] = CHANGED_EVENT
    return response


def _not_running(error: Optional[str]) -> str:
    return f"SIEM delivery is not running in this process: {error}"


def _document(
    request: Request, template: str, build: Callable[[Any], Dict[str, Any]], **ctx: Any
) -> Response:
    scheduler, error = admin.find_scheduler(request.app.state)
    if scheduler is None:
        return _fragment(
            request,
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            message=_not_running(error),
        )
    try:
        doc = build(scheduler)
    except admin.SiemAdminError as exc:
        return _fragment(request, exc.status, message=exc.message)
    return templates.TemplateResponse(
        request, template, {"doc": doc, "cap": COUNT_CAP, **ctx}
    )


MAX_REQUEUE = admin.RETARGET_ROWS_PER_TX  # the service's own per-call cap
MAX_PASTED_IDS = 1000
_ID_SEPARATORS = re.compile(r"[\s,]+")


def _no_fields(form: FormData) -> Tuple[Any, ...]:
    return ()


def _canonical_ids(values: List[Any]) -> List[str]:
    """Every value must be an id (a UUID); nothing is dropped silently."""
    ids, bad = [], 0
    for value in values:
        try:
            ids.append(str(uuid.UUID(value)) if isinstance(value, str) else "")
        except ValueError:
            ids.append("")
        bad += ids[-1] == ""
    if bad:
        raise siem_forms.FormRefused(400, f"{bad} entries are not ids")
    return ids


def _requeue_args(form: FormData) -> Tuple[Any, ...]:
    values = form.getlist("event_uuid")
    if len(values) > MAX_REQUEUE:
        raise siem_forms.FormRefused(400, f"at most {MAX_REQUEUE} event uuids per call")
    return (_canonical_ids(values),)


def _confirm_args(form: FormData) -> Tuple[Any, ...]:
    pasted = form.get("visible_ids_text")
    tokens = list(form.getlist("visible_id"))
    if isinstance(pasted, str):
        tokens += [t for t in _ID_SEPARATORS.split(pasted) if t]
    if len(tokens) > MAX_PASTED_IDS:
        raise siem_forms.FormRefused(400, f"at most {MAX_PASTED_IDS} ids per call")
    run_id = form.get("canary_run_id")
    if not isinstance(run_id, str) or not run_id:
        raise siem_forms.FormRefused(400, "canary_run_id is required")
    return run_id, _canonical_ids(tokens)


async def _act(
    request: Request,
    action: str,
    fn: Callable[..., Dict[str, Any]],
    actor: str,
    *,
    max_fields: int = 1,
    parse: Callable[[FormData], Tuple[Any, ...]] = _no_fields,
) -> Response:
    """Capped form + CSRF, then a pure parse (both refuse BEFORE any service
    call), then the service in a worker thread."""
    try:
        form = await siem_forms.read_checked_form(
            request,
            max_files=0,
            max_fields=max_fields,
            max_body=siem_forms.SIEM_ACTION_FORM_BODY,
        )
        args = parse(form)
    except siem_forms.FormRefused as refused:
        return _fragment(
            request, refused.status, action=action, message=refused.message
        )
    return await anyio.to_thread.run_sync(
        functools.partial(_perform, request, action, fn, actor, *args)
    )


def _perform(
    request: Request,
    action: str,
    fn: Callable[..., Dict[str, Any]],
    actor: str,
    *args: Any,
) -> Response:
    """Worker thread: the shared service call, its refusal status and text
    passed through unchanged (as REST does)."""
    scheduler, error = admin.find_scheduler(request.app.state)
    if scheduler is None:
        return _fragment(
            request,
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            action=action,
            message=_not_running(error),
        )
    try:
        result = fn(scheduler, actor, *args)
    except admin.SiemAdminError as exc:
        return _fragment(request, exc.status, action=action, message=exc.message)
    return _fragment(request, http_status.HTTP_200_OK, action=action, result=result)


def _abandon_confirmed(
    scheduler: Any, actor: str, destination_key: str, confirm_word: Any
) -> Dict[str, Any]:
    """The typed word is checked BEFORE the (unchanged) abandon service."""
    admin.require_abandon_confirmation(confirm_word)
    return admin.abandon_destination(scheduler, actor, destination_key)


@siem_delivery_web_router.get("/siem-delivery/partials/arming")
def siem_arming_partial(
    request: Request,
    _admin: User = Depends(dependencies.get_current_admin_user_hybrid),
) -> Response:
    return _document(request, ARMING_TEMPLATE, ops_documents.arming_document)


@siem_delivery_web_router.get("/siem-delivery/partials/recovery")
def siem_recovery_partial(
    request: Request,
    q_after: str = "",
    d_after: str = "",
    b_after: str = "",
    _admin: User = Depends(dependencies.get_current_admin_user_hybrid),
) -> Response:
    return _document(
        request,
        RECOVERY_TEMPLATE,
        lambda s: ops_documents.recovery_document(s, q_after, d_after, b_after),
        q_after=q_after,
        d_after=d_after,
        b_after=b_after,
    )


@siem_delivery_web_router.get(
    "/siem-delivery/partials/destinations/{destination_key}/abandon"
)
def siem_destination_dialog(
    request: Request,
    destination_key: str,
    _admin: User = Depends(dependencies.get_current_admin_user_hybrid),
) -> Response:
    return _document(
        request,
        DIALOG_TEMPLATE,
        lambda s: ops_documents.destination_document(s, destination_key),
    )


# --- actions: admin + TOTP elevation (while enforcement is ON) + CSRF -----------


@siem_delivery_web_router.post("/siem-delivery/canary")
async def siem_run_canary(
    request: Request, current_user: User = Depends(dependencies.require_elevation())
) -> Response:
    return await _act(request, "canary", admin.run_canary, current_user.username)


@siem_delivery_web_router.post("/siem-delivery/canary/confirm-visible")
async def siem_confirm_visible(
    request: Request, current_user: User = Depends(dependencies.require_elevation())
) -> Response:
    return await _act(
        request,
        "confirm_visible",
        admin.confirm_visible,
        current_user.username,
        max_fields=MAX_PASTED_IDS + 3,
        parse=_confirm_args,
    )


@siem_delivery_web_router.post("/siem-delivery/resume")
async def siem_resume(
    request: Request, current_user: User = Depends(dependencies.require_elevation())
) -> Response:
    return await _act(request, "resume", admin.resume, current_user.username)


@siem_delivery_web_router.post("/siem-delivery/quarantine/requeue")
async def siem_requeue(
    request: Request, current_user: User = Depends(dependencies.require_elevation())
) -> Response:
    return await _act(
        request,
        "requeue",
        admin.requeue_quarantined,
        current_user.username,
        max_fields=MAX_REQUEUE + 2,
        parse=_requeue_args,
    )


@siem_delivery_web_router.post("/siem-delivery/batches/{batch_id}/acknowledge")
async def siem_acknowledge_batch(
    batch_id: str,
    request: Request,
    current_user: User = Depends(dependencies.require_elevation()),
) -> Response:
    return await _act(
        request,
        "acknowledge",
        admin.acknowledge_batch,
        current_user.username,
        parse=lambda form: (batch_id,),
    )


@siem_delivery_web_router.post("/siem-delivery/batches/{batch_id}/rebatch")
async def siem_rebatch_batch(
    batch_id: str,
    request: Request,
    current_user: User = Depends(dependencies.require_elevation()),
) -> Response:
    return await _act(
        request,
        "rebatch",
        admin.rebatch_batch,
        current_user.username,
        parse=lambda form: (batch_id,),
    )


@siem_delivery_web_router.post("/siem-delivery/destinations/{destination_key}/retarget")
async def siem_retarget_destination(
    destination_key: str,
    request: Request,
    current_user: User = Depends(dependencies.require_elevation()),
) -> Response:
    return await _act(
        request,
        "retarget",
        admin.retarget_destination,
        current_user.username,
        parse=lambda form: (destination_key,),
    )


@siem_delivery_web_router.post("/siem-delivery/destinations/{destination_key}/abandon")
async def siem_abandon_destination(
    destination_key: str,
    request: Request,
    current_user: User = Depends(dependencies.require_elevation()),
) -> Response:
    return await _act(
        request,
        "abandon",
        _abandon_confirmed,
        current_user.username,
        max_fields=2,
        parse=lambda form: (destination_key, form.get("confirm_word")),
    )
