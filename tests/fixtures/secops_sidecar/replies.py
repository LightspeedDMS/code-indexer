"""What the ingest HTTP layer writes back for one import request."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Dict

from .google_errors import error_body


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    close: bool = False
    drop: bool = False  # close the connection WITHOUT any response (accept_then_drop)
    hold_seconds: float = 0.0  # wait this long before answering (delay with accept)
    content_type: str = "application/json"


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def error_reply(code: int, **kwargs: Any) -> Reply:
    return Reply(code, json_bytes(error_body(code, **kwargs)))


def accepted_reply() -> Reply:
    """Chronicle's success answer: HTTP 200 with an empty JSON body."""
    return Reply(HTTPStatus.OK, b"{}")
