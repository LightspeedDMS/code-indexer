"""The SIEM credential / trusted-CA forms read their body under a hard cap
BEFORE any multipart parsing: nothing beyond the cap is ever pulled or
spooled, and the file / field counts are bounded."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest
from starlette.requests import Request

from code_indexer.server.web.siem_forms import FormTooLarge, read_capped_form

CAP = 512 * 1024
CHUNK = 64 * 1024
BOUNDARY = "exampleboundary"


def _multipart(parts: List[Tuple[str, Optional[str], bytes]]) -> bytes:
    out = b""
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += (
            f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
    return out + f"--{BOUNDARY}--\r\n".encode()


class _Body:
    """An ASGI receive callable over *body*, counting the bytes pulled."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.pulled = 0

    async def __call__(self) -> Dict[str, Any]:
        chunk = self.body[self.pulled : self.pulled + CHUNK]
        self.pulled += len(chunk)
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": self.pulled < len(self.body),
        }


def _request(body: _Body, *, content_length: Optional[int]) -> Request:
    headers = [(b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    return Request(scope, body)


def _read(request: Request, **limits: Any) -> Any:
    return asyncio.run(read_capped_form(request, **limits))


def test_declared_oversize_body_is_refused_before_reading() -> None:
    body = _Body(_multipart([("f", "k.json", b"x" * (CAP + 1))]))
    with pytest.raises(FormTooLarge):
        _read(_request(body, content_length=len(body.body)), max_files=1, max_fields=2)
    assert body.pulled == 0


def test_streamed_oversize_body_stops_at_the_cap() -> None:
    body = _Body(_multipart([("f", "k.json", b"x" * (4 * 1024 * 1024))]))
    with pytest.raises(FormTooLarge):
        _read(_request(body, content_length=None), max_files=1, max_fields=2)
    assert body.pulled <= CAP + CHUNK


def test_file_and_field_counts_are_bounded() -> None:
    two_files = _multipart([("a", "a.json", b"{}"), ("b", "b.json", b"{}")])
    with pytest.raises(ValueError):
        _read(
            _request(_Body(two_files), content_length=len(two_files)),
            max_files=1,
            max_fields=2,
        )
    fields = _multipart([(f"f{i}", None, b"v") for i in range(5)])
    with pytest.raises(ValueError):
        _read(
            _request(_Body(fields), content_length=len(fields)),
            max_files=1,
            max_fields=2,
        )


def test_a_normal_form_parses() -> None:
    body = _multipart(
        [("csrf_token", None, b"t"), ("service_account_file", "k.json", b'{"a":1}')]
    )
    form = _read(
        _request(_Body(body), content_length=len(body)), max_files=1, max_fields=2
    )
    assert form["csrf_token"] == "t"
    upload = form["service_account_file"]
    assert asyncio.run(upload.read()) == b'{"a":1}'
