"""Classification of an ``events:import`` outcome (pure, no I/O).

Signatures hold only the HTTP status, the closed ``google.rpc.Code`` status
enum, the class, and a SANITISED field path.  Google's message text,
descriptions and bodies are never stored, logged or returned.
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple


class DuplicatePolicy(enum.Enum):
    """How a duplicate response is treated.  ``PROVEN_CONTRACT`` would need a
    documented Google contract proving the response acknowledges the SAME
    persisted batch; none exists, so it has no code path."""

    HALT = "halt"
    PROVEN_CONTRACT = "proven_contract"


DUPLICATE_POLICY = DuplicatePolicy.HALT

ACCEPTED = "accepted"
ROW_REJECTION = "row_rejection"
REQUEST_REJECTION = "request_rejection"
UNCLASSIFIED = "unclassified"
CREDENTIAL = "credential"
DUPLICATE_RESPONSE = "duplicate_response"
THROTTLED = "throttled"
TRANSIENT = "transient"
SYSTEMIC_CLASSES = frozenset(
    {REQUEST_REJECTION, UNCLASSIFIED, CREDENTIAL, DUPLICATE_RESPONSE}
)
# Classes meaning Google definitively refused the request (nothing ingested).
NOT_INGESTED_CLASSES = frozenset({ROW_REJECTION, REQUEST_REJECTION, CREDENTIAL})

GOOGLE_RPC_CODES = frozenset(
    {
        "OK",
        "CANCELLED",
        "UNKNOWN",
        "INVALID_ARGUMENT",
        "DEADLINE_EXCEEDED",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DATA_LOSS",
        "UNAUTHENTICATED",
    }
)
UNRECOGNISED = "<unrecognised>"
UNINDEXED = "unindexed"
_PATH_RE = re.compile(r"^[A-Za-z0-9_.\[\]*]{1,200}$")
_INDEX_RE = re.compile(r"\[\d+\]")
_EVENT_PATH_RE = re.compile(r"^(?:inline_source|inlineSource)\.events\[(\d+)\]")
_REQUEST_REJECTION_STATUSES = frozenset({404, 413, 415, 501})
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 86_400.0


def sanitize_path(raw: str) -> str:
    replaced = _INDEX_RE.sub("[*]", raw)
    return replaced if _PATH_RE.match(replaced) else UNRECOGNISED


def local_signature(reason: str, path: str) -> str:
    return f"local|{reason}|{sanitize_path(path) if path else ''}"


@dataclass(frozen=True)
class HttpOutcome:
    """An HTTP response as the classifier sees it (body never logged)."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Classification:
    cls: str
    signature: str
    indices: Optional[List[int]] = None
    retry_after_seconds: Optional[float] = None
    outcome_unknown: bool = False
    unexpected_success_body: bool = False

    @property
    def not_ingested(self) -> bool:
        return self.cls in NOT_INGESTED_CLASSES

    @property
    def systemic(self) -> bool:
        return self.cls in SYSTEMIC_CLASSES


def _error_doc(body: bytes) -> Tuple[Optional[str], List[str]]:
    """(google status or None, violation field paths); [] when unparseable."""
    try:
        doc: Any = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, []
    error = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(error, dict):
        return None, []
    status = error.get("status")
    google = status if isinstance(status, str) and status in GOOGLE_RPC_CODES else None
    if isinstance(status, str) and google is None:
        google = UNRECOGNISED
    fields: List[str] = []
    details = error.get("details")
    for detail in details if isinstance(details, list) else []:
        violations = detail.get("fieldViolations") if isinstance(detail, dict) else None
        for violation in violations if isinstance(violations, list) else []:
            name = violation.get("field") if isinstance(violation, dict) else None
            if isinstance(name, str):
                fields.append(name)
    return google, fields


def _signature(status: int, google: Optional[str], cls: str, paths: List[str]) -> str:
    primary = min(sanitize_path(p) for p in paths) if paths else UNINDEXED
    return f"{status}|{google or '-'}|{cls}|{primary}"


def _classify_400(outcome: HttpOutcome, event_count: int) -> Classification:
    google, fields = _error_doc(outcome.body)
    if not fields:
        return Classification(
            ROW_REJECTION, _signature(400, google, ROW_REJECTION, []), indices=None
        )
    indices: List[int] = []
    request_level = False
    for name in fields:
        match = _EVENT_PATH_RE.match(name)
        if match is None:
            request_level = True
        else:
            indices.append(int(match.group(1)))
    if any(i < 0 or i >= event_count for i in indices):
        return Classification(
            UNCLASSIFIED, _signature(400, google, UNCLASSIFIED, fields)
        )
    if request_level:
        return Classification(
            REQUEST_REJECTION, _signature(400, google, REQUEST_REJECTION, fields)
        )
    return Classification(
        ROW_REJECTION,
        _signature(400, google, ROW_REJECTION, fields),
        indices=sorted(set(indices)),
    )


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    lowered: Dict[str, str] = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get("retry-after")
    if raw is None:
        return None
    value = raw.strip()
    if value.isdigit():
        return min(float(value), _MAX_RETRY_AFTER_SECONDS)
    try:  # the HTTP-date form (RFC 9110 section 10.2.3)
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        return None
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(0.0, delta), _MAX_RETRY_AFTER_SECONDS)


def classify(
    outcome: Optional[HttpOutcome],
    *,
    event_count: int,
    transport_error: Optional[str] = None,
) -> Classification:
    """Classify a response, or a transport failure (*outcome* None).

    ``transport_error`` is ``connect`` (nothing transmitted: known outcome),
    or ``timeout`` / ``reset`` (the request may have been received: the
    outcome is UNKNOWN, which a later resend is counted against).
    """
    if outcome is None:
        kind = transport_error or "reset"
        return Classification(
            TRANSIENT,
            f"0|-|{TRANSIENT}|{kind}",
            outcome_unknown=kind != "connect",
        )
    status = outcome.status
    if 200 <= status < 300:
        unexpected = outcome.body.strip() not in (b"", b"{}")
        return Classification(
            ACCEPTED, f"{status}|-|{ACCEPTED}|-", unexpected_success_body=unexpected
        )
    if status == 400:
        return _classify_400(outcome, event_count)
    google, fields = _error_doc(outcome.body)
    if status in _REQUEST_REJECTION_STATUSES:
        cls = REQUEST_REJECTION
    elif status in (401, 403):
        cls = CREDENTIAL
    elif status == 409:
        cls = DUPLICATE_RESPONSE
    elif status == 429:
        return Classification(
            THROTTLED,
            _signature(status, google, THROTTLED, []),
            retry_after_seconds=_retry_after(outcome.headers),
        )
    elif status in _TRANSIENT_STATUSES:
        cls = TRANSIENT
    else:
        cls = UNCLASSIFIED
    return Classification(cls, _signature(status, google, cls, fields))
