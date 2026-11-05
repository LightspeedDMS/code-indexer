"""
Admin SIEM delivery credential and trusted CA forms on the configuration page.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

from code_indexer.server.middleware.correlation import get_correlation_id
import asyncio
import functools
import anyio
import anyio.from_thread
import anyio.to_thread
from typing import Any, Tuple
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse
from ...auth import dependencies
from ..auth import SessionData

from ._shared import (
    _require_admin_session,
    logger,
)
from .config import (
    _create_config_page_response,
)

web_router = APIRouter()


async def _read_siem_form(
    request: Request, session: SessionData, *, max_files: int, max_fields: int
) -> Tuple[Any, Any]:
    """(form, None), or (None, error page): the body is read under a hard
    cap BEFORE parsing (413 over it), counts are bounded, CSRF is checked."""
    from ..siem_forms import FormRefused, read_checked_form

    try:
        form = await read_checked_form(
            request, max_files=max_files, max_fields=max_fields
        )
    except FormRefused as refused:
        errors = None if refused.status == 403 else {"siem_delivery": refused.message}
        return None, _create_config_page_response(
            request,
            session,
            error_message=refused.message,
            validation_errors=errors,
            status_code=refused.status,
        )
    return form, None


def _siem_credential_page(
    request: Request, session: SessionData, action: Any, *args: Any
) -> Any:
    """Run one SIEM credential admin action (sync, called off the event
    loop) and build the config page: identity-only success, or the error."""
    from ...services.siem_delivery.admin import SiemAdminError

    scheduler = getattr(request.app.state, "siem_delivery_scheduler", None)
    if scheduler is None:
        message = "SIEM Delivery is not running in this process"
        return _create_config_page_response(
            request,
            session,
            error_message=message,
            validation_errors={"siem_delivery": message},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    try:
        result = action(scheduler, session.username, *args)
    except SiemAdminError as exc:
        message = f"SIEM Delivery: {exc.message}"
        return _create_config_page_response(
            request,
            session,
            error_message=message,
            validation_errors={"siem_delivery": message},
            status_code=exc.status,
        )
    ident = result["credential"]
    return _create_config_page_response(
        request,
        session,
        success_message=(
            f"SIEM Delivery service-account credential {result['change']}: "
            f"{ident['client_email']} (key id {ident['private_key_id']})"
        ),
    )


@web_router.post(
    "/config/siem_delivery/credential",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def set_siem_delivery_credential(request: Request):
    """Set or replace the SecOps service-account key (pasted or uploaded).

    Write-only: the response never carries the key, only its identity."""
    from starlette.datastructures import UploadFile

    from ...services.siem_delivery.admin import set_credential
    from ...services.siem_delivery.config_view import credential_text_from_inputs
    from ...services.siem_delivery.credential import MAX_CREDENTIAL_JSON_BYTES

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)
    form, refused = await _read_siem_form(request, session, max_files=1, max_fields=2)
    if refused is not None:
        return refused
    pasted = form.get("service_account_json")
    upload = form.get("service_account_file")
    uploaded = (
        await upload.read(MAX_CREDENTIAL_JSON_BYTES + 1)
        if isinstance(upload, UploadFile)
        else None
    )
    try:
        key_json = credential_text_from_inputs(
            pasted if isinstance(pasted, str) else "", uploaded
        )
    except ValueError as exc:
        message = f"SIEM Delivery: {exc}"
        return _create_config_page_response(
            request,
            session,
            error_message=message,
            validation_errors={"siem_delivery": message},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return await anyio.to_thread.run_sync(
        functools.partial(
            _siem_credential_page, request, session, set_credential, key_json
        )
    )


@web_router.post(
    "/config/siem_delivery/credential/remove",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def remove_siem_delivery_credential(request: Request):
    """Remove the stored SecOps service-account key."""
    from ...services.siem_delivery.admin import remove_credential

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)
    _form, refused = await _read_siem_form(request, session, max_files=0, max_fields=1)
    if refused is not None:
        return refused
    return await anyio.to_thread.run_sync(
        functools.partial(_siem_credential_page, request, session, remove_credential)
    )


def _siem_trusted_ca_page(
    request: Request, session: SessionData, action: Any, *args: Any
) -> Any:
    """Run one trusted-CA change (sync, off the event loop) and build the
    config page: the fingerprint on success, or the validation error."""
    from ...services.config_service import get_config_service
    from ...services.siem_delivery.trust import SiemTrustInvalid

    try:
        result = action(get_config_service(), session.username, *args)
    except SiemTrustInvalid as exc:
        message = f"SIEM Delivery: {exc}"
        return _create_config_page_response(
            request,
            session,
            error_message=message,
            validation_errors={"siem_delivery": message},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as exc:  # a storage failure: a clean page, never a 500 trace
        logger.error(
            "SIEM Delivery trusted CA change failed: %s",
            type(exc).__name__,
            exc_info=True,
            extra={"correlation_id": get_correlation_id()},
        )
        message = f"SIEM Delivery: the trusted CA change failed ({type(exc).__name__})"
        return _create_config_page_response(
            request,
            session,
            error_message=message,
            validation_errors={"siem_delivery": message},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return _create_config_page_response(
        request,
        session,
        success_message=(
            f"SIEM Delivery trusted CA {result['change']} "
            f"(SHA-256 {result['fingerprint']})"
        ),
    )


@web_router.post(
    "/config/siem_delivery/trusted_ca",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def set_siem_delivery_trusted_ca(request: Request):
    """Set or replace the additional trusted CA (PEM pasted or uploaded).

    It ADDS to the default trust of both outbound legs; it never disables
    verification."""
    from starlette.datastructures import UploadFile

    from ...services.siem_delivery.config_view import ca_text_from_inputs
    from ...services.siem_delivery.trust import MAX_CA_PEM_BYTES, set_trusted_ca

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)
    form, refused = await _read_siem_form(request, session, max_files=1, max_fields=2)
    if refused is not None:
        return refused
    pasted = form.get("trusted_ca_pem")
    upload = form.get("trusted_ca_file")
    uploaded = (
        await upload.read(MAX_CA_PEM_BYTES + 1)
        if isinstance(upload, UploadFile)
        else None
    )
    try:
        pem_text = ca_text_from_inputs(
            pasted if isinstance(pasted, str) else "", uploaded
        )
    except ValueError as exc:
        message = f"SIEM Delivery: {exc}"
        return _create_config_page_response(
            request,
            session,
            error_message=message,
            validation_errors={"siem_delivery": message},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return await anyio.to_thread.run_sync(
        functools.partial(
            _siem_trusted_ca_page, request, session, set_trusted_ca, pem_text
        )
    )


@web_router.post(
    "/config/siem_delivery/trusted_ca/remove",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
async def remove_siem_delivery_trusted_ca(request: Request):
    """Remove the additional trusted CA (back to the default trust only)."""
    from ...services.siem_delivery.trust import remove_trusted_ca

    session = await asyncio.to_thread(_require_admin_session, request)
    if not session:
        return HTMLResponse(content="", status_code=401)
    _form, refused = await _read_siem_form(request, session, max_files=0, max_fields=1)
    if refused is not None:
        return refused
    return await anyio.to_thread.run_sync(
        functools.partial(_siem_trusted_ca_page, request, session, remove_trusted_ca)
    )
