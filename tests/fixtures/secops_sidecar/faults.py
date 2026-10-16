"""Ingest fault modes: validation of a POST /_control/faults spec.

Every fault is consumed exactly once per ``count``, FIFO, and recorded in the
request log; ``reject_if_contains`` is the one persistent rule (until reset).
No randomness anywhere, so tests are deterministic.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

STATUS_FAULT_CODES = frozenset(
    {401, 403, 404, 409, 413, 415, 429, 500, 501, 502, 503, 504}
)
REDIRECT_CODES = frozenset({301, 302, 307, 308})
MALFORMED_KINDS = ("non_json", "truncated_json", "wrong_shape", "empty")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_REJECT_PATH = "udm.metadata.eventType"
MAX_FAULT_COUNT = 10_000
MAX_QUEUED_FAULTS = 10_000
MAX_DELAY_SECONDS = 600
MAX_RETRY_AFTER_SECONDS = 86_400
MIN_HTTP_STATUS, MAX_HTTP_STATUS = 100, 599


class FaultSpecError(ValueError):
    """An invalid fault spec; the control API answers 400."""


def _req_int(
    spec: Dict[str, Any], key: str, lo: int, hi: int, default: Optional[int] = None
) -> int:
    value = spec.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise FaultSpecError(f"{key} must be an integer in [{lo}, {hi}]")
    return value


def _req_str(spec: Dict[str, Any], key: str, default: Optional[str] = None) -> str:
    value = spec.get(key, default)
    if not isinstance(value, str) or not value:
        raise FaultSpecError(f"{key} must be a non-empty string")
    return value


def _loopback_url(spec: Dict[str, Any], key: str) -> str:
    value = _req_str(spec, key)
    parts = urlsplit(value)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        raise FaultSpecError(f"{key} must be an http loopback URL")
    return value


_INDEX = ("int", 0, MAX_FAULT_COUNT, None)
# mode -> {argument: rule}.  Rules: ("int", lo, hi, default), ("number", lo, hi),
# ("choice", allowed), ("str", default), ("bool", default), ("url",).
MODE_FIELDS: Dict[str, Dict[str, Tuple[Any, ...]]] = {
    "reject_event": {"event_index": _INDEX, "path": ("str", DEFAULT_REJECT_PATH)},
    "reject_unindexed": {},
    "reject_request": {},
    "reject_if_contains": {"product_log_id": ("str", None)},
    "status": {"code": ("choice", STATUS_FAULT_CODES)},
    "reject_mixed": {"event_index": _INDEX},
    "reject_out_of_range": {},
    "malformed_response": {
        "status": ("int", MIN_HTTP_STATUS, MAX_HTTP_STATUS, None),
        "kind": ("choice", MALFORMED_KINDS),
    },
    "success_body": {"body": ("str", None)},
    "redirect": {"code": ("choice", REDIRECT_CODES), "location": ("url",)},
    "rate_limit": {"retry_after_seconds": ("int", 0, MAX_RETRY_AFTER_SECONDS, None)},
    "delay": {"seconds": ("number", 0, MAX_DELAY_SECONDS), "accept": ("bool", False)},
    "accept_then_drop": {},
    "echo_marker": {
        "marker": ("str", None),
        "event_index": ("int", 0, MAX_FAULT_COUNT, 0),
    },
}
PERSISTENT_MODES = frozenset({"reject_if_contains"})


def _read_field(spec: Dict[str, Any], key: str, rule: Tuple[Any, ...]) -> Any:
    kind = rule[0]
    if kind == "int":
        return _req_int(spec, key, rule[1], rule[2], rule[3])
    if kind == "str":
        return _req_str(spec, key, rule[1])
    if kind == "url":
        return _loopback_url(spec, key)
    value = spec.get(key, rule[1] if kind == "bool" else None)
    if kind == "bool" and isinstance(value, bool):
        return value
    if kind == "choice" and value in rule[1] and not isinstance(value, bool):
        return value
    if (
        kind == "number"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        if rule[1] <= value <= rule[2]:
            return float(value)
    raise FaultSpecError(f"invalid {key!r} for this fault mode")


def _validate_against(
    spec: Dict[str, Any], table: Dict[str, Dict[str, Tuple[Any, ...]]]
) -> Tuple[Dict[str, Any], int]:
    """Return (normalized fault, count) for *spec* under *table*."""
    mode = spec.get("mode")
    if not isinstance(mode, str) or mode not in table:
        raise FaultSpecError(f"mode must be one of {sorted(table)}")
    fields = table[mode]
    unknown = sorted(set(spec) - set(fields) - {"mode", "count"})
    if unknown:
        raise FaultSpecError(f"unknown arguments for {mode}: {unknown}")
    count = _req_int(spec, "count", 1, MAX_FAULT_COUNT, 1)
    fault: Dict[str, Any] = {"mode": mode}
    for key, rule in fields.items():
        fault[key] = _read_field(spec, key, rule)
    return fault, count


def validate_fault(spec: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Return (normalized ingest fault, count) or raise FaultSpecError."""
    return _validate_against(spec, MODE_FIELDS)


# Token endpoint faults (POST /_control/token-faults), consumed FIFO by /token.
TOKEN_FAULT_FIELDS: Dict[str, Dict[str, Tuple[Any, ...]]] = {
    "reject": {},  # 400 invalid_grant
    "unavailable": {},  # 503
    "echo": {"marker": ("str", None)},  # 400 whose description carries the marker
}


def validate_token_fault(spec: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Return (normalized token fault, count) or raise FaultSpecError."""
    return _validate_against(spec, TOKEN_FAULT_FIELDS)
