"""SIEM capture hook inside ``insert_events`` (both backends).

* :func:`prepare_captures` runs BEFORE the audit transaction: it decides
  which events are captured and projects them (no lock is held).
* :func:`write_captures` runs INSIDE the audit transaction, after the audit
  rows: one savepointed INSERT per captured row and nothing else -- no read
  of any other SIEM table, no lock, no UDM work.
* Fail-open: a failed queue insert rolls back only its savepoint; the audit
  row still commits and the gap is COUNTED (``siem.capture_failures``) and
  logged at ERROR (rate-limited).  Nothing here ever raises into the audit
  write.

Pilot capture is decided by this process's snapshot, published by the
scheduler loop each cycle.  A snapshot older than
``CAPTURE_SNAPSHOT_MAX_AGE`` captures nothing (counted), which bounds stale
capture after a disable to 90 s.

The snapshot, counters and log-rate state are PROCESS wiring and
per-process telemetry (reported with node id and pid), not cross-request
application state: queue rows live only in the shared database.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from code_indexer.server.services.audit_events import AuditEvent
from code_indexer.server.services.siem_delivery import telemetry
from code_indexer.server.services.siem_delivery.db import Dialect
from code_indexer.server.services.siem_delivery.projection import (
    project,
    to_rfc3339_utc,
)
from code_indexer.server.services.siem_delivery.scope import (
    PILOT_ACTION_TYPES,
    is_self_report,
)
from code_indexer.server.services.siem_delivery.udm import MAPPING_VERSION

logger = logging.getLogger(__name__)

CAPTURE_SNAPSHOT_MAX_AGE = 90.0
FAILURE_LOG_WINDOW_SECONDS = 60.0

INSERT_FAILED = "insert_failed"
TRANSACTION_FAILED = "transaction_failed"
SELF_REPORT_WITHOUT_DESTINATION = "self_report_without_destination"
DESTINATION_NOT_SELF_REPORT = "explicit_destination_not_self_report"

BOUNDARY_KINDS = frozenset(
    {"enable", "disable", "clear", "destination_change", "reset", "other_siem_change"}
)


class SiemTarget(NamedTuple):
    """Explicit destination of a self-report row (and its boundary kind)."""

    destination_key: str
    boundary_kind: Optional[str] = None


# Mapping event_uuid -> SiemTarget, or None = "self-report deliberately not
# captured: no destination exists".
SiemDestinations = Mapping[str, Optional[SiemTarget]]


@dataclass(frozen=True)
class CaptureSnapshot:
    loaded: bool
    active: bool
    destination_key: Optional[str]
    read_started_at: float  # monotonic, taken BEFORE the cycle's reads


NOT_LOADED = CaptureSnapshot(False, False, None, 0.0)


@dataclass(frozen=True)
class PreparedCapture:
    event_uuid: str
    destination_key: str
    occurred_at: str
    action_type: str
    correlation_id: Optional[str]
    payload_json: Optional[str]
    projection_error: Optional[str]
    mapping_version: int
    boundary_kind: Optional[str]


class _CaptureProcessState:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.lock = threading.Lock()
        self.clock = clock
        self.snapshot = NOT_LOADED
        self.max_age = CAPTURE_SNAPSHOT_MAX_AGE
        self.failures: Counter = Counter()
        self.skipped_snapshot_expired = 0
        self.log_windows: Dict[Tuple[str, str], List[float]] = {}


_state = _CaptureProcessState()


def publish_capture_state(snapshot: CaptureSnapshot) -> None:
    with _state.lock:
        _state.snapshot = snapshot


def capture_state() -> CaptureSnapshot:
    with _state.lock:
        return _state.snapshot


def snapshot_age_seconds() -> Optional[float]:
    with _state.lock:
        snap = _state.snapshot
        if not snap.loaded:
            return None
        return _state.clock() - snap.read_started_at


def capture_failures_since_boot() -> Dict[str, int]:
    with _state.lock:
        return dict(_state.failures)


def capture_skipped_snapshot_expired() -> int:
    with _state.lock:
        return _state.skipped_snapshot_expired


def reset_capture_state_for_tests(
    clock: Callable[[], float] = time.monotonic,
) -> None:
    global _state
    _state = _CaptureProcessState(clock)


def monotonic_now() -> float:
    return _state.clock()


def record_capture_failure(
    reason: str,
    *,
    action_type: str,
    event_uuid: str,
    correlation_id: Optional[str],
    exc: Optional[BaseException] = None,
) -> None:
    """Count one capture gap; ERROR at most once per window per key."""
    key = (reason, action_type)
    with _state.lock:
        _state.failures[reason] += 1
        now = _state.clock()
        window = _state.log_windows.get(key)
        emit, suppressed = True, 0
        if window is not None and now - window[0] < FAILURE_LOG_WINDOW_SECONDS:
            window[1] += 1
            emit = False
        else:
            suppressed = int(window[1]) if window is not None else 0
            if len(_state.log_windows) > 512:
                _state.log_windows.clear()
            _state.log_windows[key] = [now, 0]
    telemetry.add_counter("capture_failures", 1, {"reason": reason})
    if emit:
        logger.error(
            "SIEM capture failed: reason=%s action_type=%s event_uuid=%s "
            "correlation_id=%s error_class=%s pid=%d suppressed_since_last=%d",
            reason,
            action_type,
            event_uuid,
            correlation_id,
            type(exc).__name__ if exc is not None else None,
            os.getpid(),
            suppressed,
        )


def _pilot_destination(snapshot: CaptureSnapshot, action_type: str) -> Optional[str]:
    if action_type not in PILOT_ACTION_TYPES:
        return None
    if not (snapshot.loaded and snapshot.active and snapshot.destination_key):
        return None
    with _state.lock:
        age = _state.clock() - snapshot.read_started_at
        if age > _state.max_age:
            _state.skipped_snapshot_expired += 1
            expired = True
        else:
            expired = False
    if expired:
        telemetry.add_counter("capture_skipped_snapshot_expired", 1)
        return None
    return snapshot.destination_key


def prepare_captures(
    events: Sequence[AuditEvent], siem_destinations: Optional[SiemDestinations]
) -> List[PreparedCapture]:
    """Decide and project, OUTSIDE the audit transaction.  Never raises."""
    snapshot = capture_state()
    out: List[PreparedCapture] = []
    for event in events:
        try:
            prepared = _prepare_one(event, snapshot, siem_destinations)
        except Exception as exc:  # noqa: BLE001 - capture is fail-open
            record_capture_failure(
                "prepare_failed",
                action_type=event.action_type,
                event_uuid=event.event_uuid,
                correlation_id=event.correlation_id,
                exc=exc,
            )
            continue
        if prepared is not None:
            out.append(prepared)
    return out


def _prepare_one(
    event: AuditEvent,
    snapshot: CaptureSnapshot,
    siem_destinations: Optional[SiemDestinations],
) -> Optional[PreparedCapture]:
    boundary: Optional[str] = None
    if siem_destinations is not None and event.event_uuid in siem_destinations:
        if not is_self_report(event):
            record_capture_failure(
                DESTINATION_NOT_SELF_REPORT,
                action_type=event.action_type,
                event_uuid=event.event_uuid,
                correlation_id=event.correlation_id,
            )
            return None
        target = siem_destinations[event.event_uuid]
        if target is None:
            return None  # no destination exists: nothing to report to
        destination: Optional[str] = target.destination_key
        boundary = target.boundary_kind
    elif is_self_report(event):
        # Only reachable on the writer path, where the explicit destination
        # was lost: counted, never guessed.
        record_capture_failure(
            SELF_REPORT_WITHOUT_DESTINATION,
            action_type=event.action_type,
            event_uuid=event.event_uuid,
            correlation_id=event.correlation_id,
        )
        return None
    else:
        destination = _pilot_destination(snapshot, event.action_type)
    if destination is None:
        return None
    payload, error = project(event)
    return PreparedCapture(
        event_uuid=event.event_uuid,
        destination_key=destination,
        occurred_at=to_rfc3339_utc(event.occurred_at) or event.occurred_at,
        action_type=event.action_type,
        correlation_id=event.correlation_id,
        payload_json=payload,
        projection_error=error,
        mapping_version=MAPPING_VERSION,
        boundary_kind=boundary,
    )


def _insert_sql(dialect: Dialect) -> str:
    now = dialect.now_sql
    return dialect.sql(
        "INSERT INTO siem_delivery_queue (event_uuid, destination_key, "
        "occurred_at, action_type, event_payload, projection_error, status, "
        "attempts, next_attempt_at, mapping_version, boundary_kind, created_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, {now}, ?, ?, {now})"
    )


def write_captures(
    conn: Any, prepared: Sequence[PreparedCapture], dialect: Dialect
) -> None:
    """INSIDE the audit transaction: one savepointed INSERT per row."""
    if not prepared:
        return
    sql = _insert_sql(dialect)
    for capture in prepared:
        params = (
            capture.event_uuid,
            capture.destination_key,
            capture.occurred_at,
            capture.action_type,
            capture.payload_json,
            capture.projection_error,
            capture.mapping_version,
            capture.boundary_kind,
        )
        try:
            with dialect.savepoint(conn, "siem_capture"):
                conn.execute(sql, params)
        except Exception as exc:  # noqa: BLE001 - capture is fail-open by design
            record_capture_failure(
                INSERT_FAILED,
                action_type=capture.action_type,
                event_uuid=capture.event_uuid,
                correlation_id=capture.correlation_id,
                exc=exc,
            )


_retry_scope = threading.local()


@contextmanager
def retried_attempt() -> Iterator[None]:
    """Mark an audit write the caller WILL retry (the writer's first attempt
    at a multi-row batch, retried row by row on failure): a transaction
    failure inside it is not a capture gap yet -- the retry that finally
    fails counts it.  Thread-scoped: the write runs on the calling thread."""
    previous = getattr(_retry_scope, "active", False)
    _retry_scope.active = True
    try:
        yield
    finally:
        _retry_scope.active = previous


def record_transaction_failure(
    prepared: Sequence[PreparedCapture], exc: Optional[BaseException] = None
) -> None:
    """The whole audit transaction failed: every prepared capture is a gap
    (unless the caller will retry the write, see :func:`retried_attempt`)."""
    if getattr(_retry_scope, "active", False):
        return
    for capture in prepared:
        record_capture_failure(
            TRANSACTION_FAILED,
            action_type=capture.action_type,
            event_uuid=capture.event_uuid,
            correlation_id=capture.correlation_id,
            exc=exc,
        )
