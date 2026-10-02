"""Small HTTP helpers shared by the ingest and control listeners."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs

# A client that already hung up (timeout tests, accept_then_drop) makes the
# write fail; that is expected and never an error of the sidecar.
CLIENT_GONE_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def send_bytes(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str = "application/json",
    headers: Optional[Mapping[str, str]] = None,
) -> bool:
    """Write one complete response; return False when the client was gone."""
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()
        return True
    except CLIENT_GONE_ERRORS:
        handler.close_connection = True
        return False


def send_json(
    handler: BaseHTTPRequestHandler,
    status: int,
    payload: Any,
    headers: Optional[Mapping[str, str]] = None,
) -> bool:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return send_bytes(handler, status, body, headers=headers)


def content_length(handler: BaseHTTPRequestHandler) -> Optional[int]:
    """The declared Content-Length, or None when absent or not a number."""
    raw = handler.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def read_bounded_body(handler: BaseHTTPRequestHandler, limit: int) -> bytes:
    """Read at most *limit* bytes of the declared body (never more)."""
    declared = content_length(handler) or 0
    return handler.rfile.read(min(declared, limit))


def read_json_object(
    handler: BaseHTTPRequestHandler, limit: int
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse a small JSON object body: (object, None) or (None, error text)."""
    declared = content_length(handler)
    if declared is None:
        return None, "Content-Length required"
    if declared > limit:
        return None, f"body larger than {limit} bytes"
    raw = handler.rfile.read(declared)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "body is not valid JSON"
    if not isinstance(parsed, dict):
        return None, "body must be a JSON object"
    return parsed, None


def query_params(raw_query: str) -> Dict[str, str]:
    """Single-valued query parameters (the last value wins)."""
    return {k: v[-1] for k, v in parse_qs(raw_query, keep_blank_values=True).items()}


def int_param(params: Mapping[str, str], name: str, default: int) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    value = int(raw)  # ValueError -> caller answers 400
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value
