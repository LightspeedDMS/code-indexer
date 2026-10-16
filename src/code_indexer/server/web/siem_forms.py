"""Bounded form reading for the SIEM Delivery credential and trusted-CA forms.

``request.form()`` spools every file part before a route can check any size
and accepts up to 1000 files.  These forms instead read the body under a
hard total cap BEFORE parsing: a declared ``Content-Length`` over the cap is
refused without reading, and a streamed body is cut off as soon as it
crosses the cap (nothing beyond it is pulled or spooled).  File and field
counts are bounded to what each form carries.
"""

from __future__ import annotations

from typing import AsyncGenerator

from starlette.datastructures import FormData
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser
from starlette.requests import Request

MAX_SIEM_FORM_BODY = 512 * 1024


class FormTooLarge(Exception):
    """The request body exceeds the form's cap (HTTP 413)."""


async def read_capped_form(
    request: Request,
    *,
    max_files: int,
    max_fields: int,
    max_body: int = MAX_SIEM_FORM_BODY,
) -> FormData:
    """The parsed form; raises :class:`FormTooLarge` over *max_body* and
    ValueError for a malformed form or too many files / fields."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_body:
        raise FormTooLarge()

    async def _capped() -> AsyncGenerator[bytes, None]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_body:
                raise FormTooLarge()
            yield chunk

    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("multipart/form-data"):
            return await MultiPartParser(
                request.headers,
                _capped(),
                max_files=max_files,
                max_fields=max_fields,
                max_part_size=max_body,
            ).parse()
        if content_type.startswith("application/x-www-form-urlencoded"):
            form = await FormParser(request.headers, _capped()).parse()
            if len(form) > max_fields:
                raise ValueError("too many form fields")
            return form
    except MultiPartException as exc:
        raise ValueError(str(exc)) from None
    raise ValueError("unsupported form content type")


SIEM_ACTION_FORM_BODY = 64 * 1024  # the operator action forms carry ids only


class FormRefused(Exception):
    """A SIEM form refused before any service call (HTTP status + message)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def read_checked_form(
    request: Request,
    *,
    max_files: int,
    max_fields: int,
    max_body: int = MAX_SIEM_FORM_BODY,
) -> FormData:
    """The parsed form, read under the hard cap (413) with bounded counts
    (400) and a valid CSRF token (403); raises :class:`FormRefused`."""
    try:
        form = await read_capped_form(
            request, max_files=max_files, max_fields=max_fields, max_body=max_body
        )
    except FormTooLarge:
        raise FormRefused(
            413, f"SIEM Delivery: the upload is larger than {max_body // 1024} KiB"
        ) from None
    except ValueError:
        raise FormRefused(400, "SIEM Delivery: the form is malformed") from None
    # Resolved at call time: the CSRF check is whatever web.routes provides.
    from . import routes as _web_routes

    csrf = form.get("csrf_token")
    if not _web_routes.validate_login_csrf_token(
        request, csrf if isinstance(csrf, str) else None
    ):
        raise FormRefused(403, "Invalid CSRF token")
    return form
