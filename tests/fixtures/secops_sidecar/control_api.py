"""Control and inspection API (control port, loopback only).

Never returns an issued token, an Authorization header value, or key
material.  Routes are a static table: (method, path) -> handler function.
"""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Tuple
from urllib.parse import urlsplit

from . import netguard
from .faults import PERSISTENT_MODES, validate_fault, validate_token_fault
from .state import CapacityExceeded
from .http_util import (
    int_param,
    query_params,
    read_json_object,
    send_bytes,
    send_json,
)

if TYPE_CHECKING:
    from .server import Sidecar

# (status, payload) returned by every GET route; POST routes also get the body.
RouteResult = Tuple[int, Any]
CONTROL_BODY_LIMIT = 64 * 1024


def _health(sidecar: "Sidecar", params: Mapping[str, str]) -> RouteResult:
    return 200, {
        "ingest_listening": sidecar.ingest_listening,
        "received_count": sidecar.state.received_count(),
    }


def _counts(sidecar: "Sidecar", params: Mapping[str, str]) -> RouteResult:
    return 200, {
        **sidecar.state.counts(),
        "outbound_connects": netguard.outbound_connects(),
    }


def _received(sidecar: "Sidecar", params: Mapping[str, str]) -> RouteResult:
    since = int_param(params, "since_seq", 0)
    events = [e.as_dict() for e in sidecar.state.events_snapshot() if e.seq > since]
    return 200, {"events": events}


def _requests(sidecar: "Sidecar", params: Mapping[str, str]) -> RouteResult:
    since = int_param(params, "since_seq", 0)
    return 200, {"requests": sidecar.state.requests_since(since)}


def _send_request_body(handler: "ControlHandler", seq: int) -> None:
    body = handler.sidecar.state.request_body(seq)
    if body is None:
        send_json(handler, 404, {"error": f"no retained body for seq {seq}"})
        return
    send_bytes(handler, 200, body, content_type="application/octet-stream")


def _search(sidecar: "Sidecar", params: Mapping[str, str]) -> RouteResult:
    """ValueError (no filter, unknown filter) becomes HTTP 400 in do_GET."""
    filters = {k: v for k, v in params.items() if v}
    results = [e.as_dict() for e in sidecar.state.search(filters)]
    return 200, {"results": results}


_VISIBILITY_KEYS = {
    "hide_product_log_id": "product_log_id",
    "hide_event_type": "event_type",
}


def _visibility(sidecar: "Sidecar", body: Dict[str, Any]) -> RouteResult:
    if len(body) != 1 or next(iter(body)) not in _VISIBILITY_KEYS:
        raise ValueError(f"exactly one of {sorted(_VISIBILITY_KEYS)} is required")
    key, value = next(iter(body.items()))
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    sidecar.state.add_hide_rule(_VISIBILITY_KEYS[key], value)
    return 200, {"hidden": {key: value}}


def _list_faults(sidecar: "Sidecar", params: Mapping[str, str]) -> RouteResult:
    return 200, sidecar.state.faults_listing()


def _queue_fault(sidecar: "Sidecar", body: Dict[str, Any]) -> RouteResult:
    """FaultSpecError (a ValueError) and CapacityExceeded become HTTP 400."""
    fault, count = validate_fault(body)
    try:
        sidecar.state.queue_fault(fault, count, fault["mode"] in PERSISTENT_MODES)
    except CapacityExceeded as exc:
        raise ValueError(str(exc)) from exc
    return 200, {"queued": fault, "count": count}


def _queue_token_fault(sidecar: "Sidecar", body: Dict[str, Any]) -> RouteResult:
    """FaultSpecError (a ValueError) and CapacityExceeded become HTTP 400."""
    fault, count = validate_token_fault(body)
    try:
        sidecar.state.queue_token_fault(fault, count)
    except CapacityExceeded as exc:
        raise ValueError(str(exc)) from exc
    return 200, {"queued": fault, "count": count}


def _outage(sidecar: "Sidecar", body: Dict[str, Any]) -> RouteResult:
    """refuse: close the ingest listener; end: reopen it on the same port."""
    mode = body.get("mode")
    if mode == "refuse":
        sidecar.refuse_ingest()
    elif mode == "end":
        sidecar.restore_ingest()
    else:
        raise ValueError('mode must be "refuse" or "end"')
    return 200, {"ingest_listening": sidecar.ingest_listening}


def _get_config(sidecar: "Sidecar", params: Mapping[str, str]) -> RouteResult:
    return 200, {"duplicate_mode": sidecar.state.get_duplicate_mode()}


def _set_config(sidecar: "Sidecar", body: Dict[str, Any]) -> RouteResult:
    """ValueError (unknown key or mode) becomes HTTP 400 in do_POST."""
    unknown = sorted(set(body) - {"duplicate_mode"})
    if unknown:
        raise ValueError(f"unknown config keys: {unknown}")
    sidecar.state.set_duplicate_mode(str(body.get("duplicate_mode", "")))
    return 200, {"duplicate_mode": sidecar.state.get_duplicate_mode()}


def _reset(sidecar: "Sidecar", body: Dict[str, Any]) -> RouteResult:
    """ValueError (unknown key, non-bool keep_tokens) becomes HTTP 400."""
    unknown = sorted(set(body) - {"keep_tokens"})
    if unknown:
        raise ValueError(f"unknown reset keys: {unknown}")
    keep_tokens = body.get("keep_tokens", False)
    if not isinstance(keep_tokens, bool):
        raise ValueError("keep_tokens must be a boolean")
    sidecar.state.reset(keep_tokens=keep_tokens)
    sidecar.restore_ingest()  # reset also ends an outage
    return 200, {"reset": True}


GET_ROUTES: Dict[str, Callable[["Sidecar", Mapping[str, str]], RouteResult]] = {
    "/_control/health": _health,
    "/_control/received": _received,
    "/_control/requests": _requests,
    "/_control/config": _get_config,
    "/_control/search": _search,
    "/_control/faults": _list_faults,
    "/_control/counts": _counts,
}
REQUEST_BODY_PATH = re.compile(r"^/_control/requests/(\d+)/body$")

POST_ROUTES: Dict[str, Callable[["Sidecar", Dict[str, Any]], RouteResult]] = {
    "/_control/reset": _reset,
    "/_control/config": _set_config,
    "/_control/visibility": _visibility,
    "/_control/faults": _queue_fault,
    "/_control/token-faults": _queue_token_fault,
    "/_control/outage": _outage,
}


class ControlHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "secops-sidecar-control"

    @property
    def sidecar(self) -> "Sidecar":
        return self.server.sidecar  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Silence per-request access logging."""

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        body_match = REQUEST_BODY_PATH.match(parts.path)
        if body_match is not None:
            _send_request_body(self, int(body_match.group(1)))
            return
        route = GET_ROUTES.get(parts.path)
        if route is None:
            send_json(self, 404, {"error": f"no such control route: {parts.path}"})
            return
        try:
            status, payload = route(self.sidecar, query_params(parts.query))
        except ValueError as exc:
            status, payload = 400, {"error": str(exc)}
        send_json(self, status, payload)

    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        body, error = read_json_object(self, CONTROL_BODY_LIMIT)
        if body is None:
            self.close_connection = True  # an unread body must not leak into keep-alive
            send_json(self, 400, {"error": error})
            return
        route = POST_ROUTES.get(parts.path)
        if route is None:
            send_json(self, 404, {"error": f"no such control route: {parts.path}"})
            return
        try:
            status, payload = route(self.sidecar, body)
        except ValueError as exc:
            status, payload = 400, {"error": str(exc)}
        send_json(self, status, payload)
