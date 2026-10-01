"""In-memory sidecar state: one lock guards everything.

Bounds (deliberate and test-visible, never unbounded growth):
  MAX_STORED_EVENTS         accepted events kept (imports answer 507 past it)
  MAX_REQUEST_LOG           import requests remembered (507 past it)
  MAX_RETAINED_BODY_BYTES   raw request bytes kept; a body past the bound is
                            NOT kept and its record says body_retained=False
  MAX_ISSUED_TOKENS         live issued tokens (expired ones are purged first)
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .faults import MAX_QUEUED_FAULTS

MAX_STORED_EVENTS = 500_000
MAX_REQUEST_LOG = 100_000
MAX_RETAINED_BODY_BYTES = 1 << 30
MAX_ISSUED_TOKENS = 100_000
TOKEN_ENTROPY_BYTES = 32
DUPLICATE_MODES = ("ok_noop", "already_exists")
DEFAULT_DUPLICATE_MODE = "ok_noop"
SEARCH_RESULT_LIMIT = 1_000
# Deliberate rule breakers for the negative-control self-tests ONLY.
SELF_TEST_DEFECTS = (
    "store_before_validate",
    "no_dedup",
    "search_ignores_hide",
    "search_includes_rejected",
)
HIDE_RULE_KINDS = ("product_log_id", "event_type")
# Search query parameter -> the UDM metadata field it matches.
SEARCH_FILTER_FIELDS = {
    "product_log_id": "productLogId",
    "event_type": "eventType",
    "product_event_type": "productEventType",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    batch_sha256: str
    received_at: str
    event_index: int
    udm: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RequestRecord:
    seq: int
    path: str
    http_status_returned: Optional[int]
    body_bytes: int
    event_count: Optional[int]
    body_sha256: str
    auth_ok: bool
    fault_applied: Optional[str]
    duplicate: bool
    body_retained: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CapacityExceeded(Exception):
    """A deliberate store bound is reached; the import answers 507."""


class FaultQueue:
    """Bounded FIFO of [fault, remaining uses]; the owner holds the lock."""

    def __init__(self) -> None:
        self.entries: List[List[Any]] = []

    def push(self, fault: Dict[str, Any], count: int) -> None:
        if count < 1:
            raise ValueError("fault count must be >= 1")
        if len(self.entries) >= MAX_QUEUED_FAULTS:
            raise CapacityExceeded("fault queue bound reached")
        self.entries.append([dict(fault), count])

    def pop(self) -> Optional[Dict[str, Any]]:
        """Consume one use of the head fault, or None when empty."""
        if not self.entries:
            return None
        head = self.entries[0]
        head[1] -= 1
        if head[1] <= 0:
            self.entries.pop(0)
        return dict(head[0])

    def listing(self) -> List[Dict[str, Any]]:
        return [{**fault, "remaining": n} for fault, n in self.entries]


class SidecarState:
    def __init__(self, self_test_defect: Optional[str] = None) -> None:
        if self_test_defect is not None and self_test_defect not in SELF_TEST_DEFECTS:
            raise ValueError(f"self_test_defect must be one of {SELF_TEST_DEFECTS}")
        # Deliberately broken rule, ONLY for the negative-control self-tests.
        self.self_test_defect = self_test_defect
        self._rejected_events: List[StoredEvent] = []
        self.lock = threading.RLock()
        self._seq = 0
        self._tokens: Dict[str, float] = {}
        self._events: List[StoredEvent] = []
        self._requests: List[RequestRecord] = []
        self._bodies: Dict[int, bytes] = {}
        self._body_bytes = 0
        self._dedup: Set[str] = set()
        self._hide_rules: Set[Tuple[str, str]] = set()
        self._ingest_faults = FaultQueue()
        self._token_faults = FaultQueue()
        self._poison_ids: Set[str] = set()  # reject_if_contains rules
        self._duplicate_mode = DEFAULT_DUPLICATE_MODE

    def next_seq(self) -> int:
        with self.lock:
            self._seq += 1
            return self._seq

    def issue_token(self, ttl_seconds: int) -> str:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        token = secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
        now = time.monotonic()
        with self.lock:
            for expired in [t for t, exp in self._tokens.items() if exp <= now]:
                del self._tokens[expired]
            if len(self._tokens) >= MAX_ISSUED_TOKENS:
                raise CapacityExceeded("issued-token bound reached")
            self._tokens[token] = now + ttl_seconds
        return token

    def token_valid(self, token: str) -> bool:
        with self.lock:
            expiry = self._tokens.get(token)
            return expiry is not None and time.monotonic() < expiry

    def get_duplicate_mode(self) -> str:
        with self.lock:
            return self._duplicate_mode

    def set_duplicate_mode(self, mode: str) -> None:
        if mode not in DUPLICATE_MODES:
            raise ValueError(f"duplicate_mode must be one of {DUPLICATE_MODES}")
        with self.lock:
            self._duplicate_mode = mode

    # -- request log --------------------------------------------------------
    def record_request(self, record: RequestRecord, body: bytes) -> RequestRecord:
        """Append *record*; keep *body* only within the retained-bytes bound."""
        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        with self.lock:
            if len(self._requests) >= MAX_REQUEST_LOG:
                raise CapacityExceeded("request log bound reached")
            retained = self._body_bytes + len(body) <= MAX_RETAINED_BODY_BYTES
            if retained:
                self._bodies[record.seq] = body
                self._body_bytes += len(body)
            stored = replace(record, body_retained=retained)
            self._requests.append(stored)
            return stored

    def requests_since(self, since_seq: int) -> List[Dict[str, Any]]:
        with self.lock:
            return [r.as_dict() for r in self._requests if r.seq > since_seq]

    def request_body(self, seq: int) -> Optional[bytes]:
        with self.lock:
            return self._bodies.get(seq)

    def request_count(self) -> int:
        with self.lock:
            return len(self._requests)

    # -- accepted events (all-or-nothing) ------------------------------------
    def is_duplicate(self, batch_sha256: str) -> bool:
        if self.self_test_defect == "no_dedup":
            return False
        with self.lock:
            return batch_sha256 in self._dedup

    def note_rejected(self, seq: int, events: List[Dict[str, Any]]) -> None:
        """Only under the search_includes_rejected defect: remember a rejection."""
        if self.self_test_defect != "search_includes_rejected":
            return
        received_at = utc_now_iso()
        with self.lock:
            for index, event in enumerate(events):
                self._rejected_events.append(
                    StoredEvent(seq, "", received_at, index, dict(event["udm"]))
                )

    def accept_batch(
        self, seq: int, batch_sha256: str, events: List[Dict[str, Any]]
    ) -> None:
        """Store every event of the batch, or nothing at all."""
        received_at = utc_now_iso()
        new = [
            StoredEvent(seq, batch_sha256, received_at, index, dict(event["udm"]))
            for index, event in enumerate(events)
        ]
        with self.lock:
            if len(self._events) + len(new) > MAX_STORED_EVENTS:
                raise CapacityExceeded("received-store bound reached")
            self._events.extend(new)
            self._dedup.add(batch_sha256)

    def events_snapshot(self) -> List[StoredEvent]:
        with self.lock:
            return list(self._events)

    def received_count(self) -> int:
        with self.lock:
            return len(self._events)

    # -- ingest fault queue (FIFO, each fault consumed once per count) --------
    def queue_fault(self, fault: Dict[str, Any], count: int, persistent: bool) -> None:
        if persistent and not fault.get("product_log_id"):
            raise ValueError("a persistent fault needs a product_log_id")
        with self.lock:
            if persistent:
                self._poison_ids.add(str(fault["product_log_id"]))
                return
            self._ingest_faults.push(fault, count)

    def pop_fault(self) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self._ingest_faults.pop()

    def queue_token_fault(self, fault: Dict[str, Any], count: int) -> None:
        with self.lock:
            self._token_faults.push(fault, count)

    def pop_token_fault(self) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self._token_faults.pop()

    def faults_listing(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "queued": self._ingest_faults.listing(),
                "persistent": [
                    {"mode": "reject_if_contains", "product_log_id": p}
                    for p in sorted(self._poison_ids)
                ],
            }

    def counts(self) -> Dict[str, int]:
        """Store sizes for /_control/counts (never any token or body)."""
        with self.lock:
            return {
                "received_count": len(self._events),
                "request_count": len(self._requests),
                "issued_token_count": len(self._tokens),
                "queued_fault_count": len(self._ingest_faults.entries),
                "hide_rule_count": len(self._hide_rules),
            }

    def poison_ids(self) -> Set[str]:
        with self.lock:
            return set(self._poison_ids)

    # -- visibility and search (the SecOps UDM-search stand-in) --------------
    def add_hide_rule(self, kind: str, value: str) -> None:
        if kind not in HIDE_RULE_KINDS or not value:
            raise ValueError(f"hide rule must be one of {HIDE_RULE_KINDS} with a value")
        with self.lock:
            self._hide_rules.add((kind, value))

    def _hidden(self, event: StoredEvent) -> bool:
        metadata = event.udm.get("metadata", {})
        return ("product_log_id", metadata.get("productLogId")) in self._hide_rules or (
            "event_type",
            metadata.get("eventType"),
        ) in self._hide_rules

    def search(self, filters: Dict[str, str]) -> List[StoredEvent]:
        """Stored, VISIBLE events matching every filter (bounded result)."""
        if not filters or any(k not in SEARCH_FILTER_FIELDS for k in filters):
            raise ValueError(f"filters must be among {sorted(SEARCH_FILTER_FIELDS)}")
        found: List[StoredEvent] = []
        ignore_hide = self.self_test_defect == "search_ignores_hide"
        with self.lock:
            for event in self._events + self._rejected_events:
                if self._hidden(event) and not ignore_hide:
                    continue
                metadata = event.udm.get("metadata", {})
                if all(
                    metadata.get(SEARCH_FILTER_FIELDS[k]) == v
                    for k, v in filters.items()
                ):
                    found.append(event)
                    if len(found) >= SEARCH_RESULT_LIMIT:
                        break
        return found

    def reset(self, keep_tokens: bool = False) -> None:
        """Clear all state; keep_tokens leaves issued tokens valid so a client's
        cached credential survives a per-test reset (as a real tenant would)."""
        with self.lock:
            if not keep_tokens:
                self._tokens.clear()
            self._events.clear()
            self._requests.clear()
            self._bodies.clear()
            self._body_bytes = 0
            self._dedup.clear()
            self._hide_rules.clear()
            self._rejected_events.clear()
            self._ingest_faults = FaultQueue()
            self._token_faults = FaultQueue()
            self._poison_ids.clear()
            self._duplicate_mode = DEFAULT_DUPLICATE_MODE
