"""The single audit capture path: binding, delivery, fail-open policy, counter.

Emitters call :func:`capture` / :func:`capture_system` (or their ``_async``
variants from ``async def`` code); code that already holds an
:class:`AuditEvent` calls :func:`record` / :func:`record_async`.

Delivery is chosen by the action type's catalog entry, never by the caller:

- DURABLE: written synchronously, in its own transaction, on the calling
  thread, before ``record`` returns (``AuditLogService.insert_events``).
- QUEUED: handed to the service's async writer thread (high-volume
  authentication activity).

Fail policy: ALWAYS PROCEED.  No audit failure ever raises into the caller or
blocks the action.  Every lost record increments a per-process counter
(surfaced on ``/health``) and logs a rate-limited ERROR that carries the
action type, event uuid, correlation id and the exception CLASS -- never
``details``, target values, or the exception message (driver messages can
echo row values).

Binding: the server marks itself at app construction and binds the ONE
lifespan-owned ``AuditLogService`` at startup.  No manager holds its own
audit reference, so a manager built per request can never write to a
disconnected store.  A process that was never marked (standalone CLI) has no
store and captures nothing, by design.  A marked process with nothing bound
is a wiring defect: every capture is a counted drop.

The binding, node id, counter and log-rate state are PROCESS wiring and
per-process telemetry (reported together with the node id), not
cross-request application state; audit rows themselves live only in the
shared store.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from collections import Counter
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

import anyio

from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    AuditEvent,
    AuditEventInvalid,
    Delivery,
    SystemComponent,
    build_event,
    build_system_event,
    process_node_id,
    set_process_node_id,
)

if TYPE_CHECKING:
    from code_indexer.server.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

# Situations (the leading text of each ERROR line).
WRITE_FAILED = "audit record not written"
QUEUE_SATURATED = "audit queue saturated"
WRITER_NOT_RUNNING = "audit writer not running"
EVENT_REJECTED = "audit event rejected"
SERVICE_UNRESOLVABLE = "audit service unresolvable (AuditServiceUnresolvable)"
ON_EVENT_LOOP = "durable audit emitted on the event loop"
WRITER_STOPPED = "audit records not written when the writer stopped"

DROP_LOG_WINDOW_SECONDS = 60.0
# Bound on distinct (situation, action_type) rate-limit windows kept.
_MAX_RATE_LIMIT_KEYS = 512


class AuditServiceUnresolvable(RuntimeError):
    """A server process captured an event with no audit service bound."""


class _DropReporter:
    """Counts lost records and rate-limits their ERROR lines.

    The first occurrence per (situation, action_type) logs at once; later
    ones inside the window are folded into the next line, which carries the
    suppressed count.  The counter counts every drop regardless.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._dropped = 0
        # key -> [window_start, suppressed_count]
        self._windows: Dict[Tuple[str, str], List[float]] = {}

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def _admit(self, key: Tuple[str, str]) -> Tuple[bool, int]:
        now = self._clock()
        state = self._windows.get(key)
        if state is None:
            if len(self._windows) >= _MAX_RATE_LIMIT_KEYS:
                self._windows = {
                    k: v
                    for k, v in self._windows.items()
                    if now - v[0] < DROP_LOG_WINDOW_SECONDS
                }
                if len(self._windows) >= _MAX_RATE_LIMIT_KEYS:
                    self._windows.clear()
            self._windows[key] = [now, 0]
            return True, 0
        if now - state[0] >= DROP_LOG_WINDOW_SECONDS:
            suppressed = int(state[1])
            state[0], state[1] = now, 0
            return True, suppressed
        state[1] += 1
        return False, 0

    def report(
        self,
        situation: str,
        *,
        action_type: str,
        event_uuid: Optional[str] = None,
        correlation_id: Optional[str] = None,
        exc: Optional[BaseException] = None,
        field: Optional[str] = None,
        counted: bool = True,
    ) -> None:
        with self._lock:
            if counted:
                self._dropped += 1
            emit, suppressed = self._admit((situation, action_type))
        if not emit:
            return
        logger.error(
            "%s: action_type=%s event_uuid=%s correlation_id=%s "
            "error_class=%s sqlstate=%s field=%s suppressed_since_last=%d",
            situation,
            action_type,
            event_uuid,
            correlation_id,
            type(exc).__name__ if exc is not None else None,
            getattr(exc, "sqlstate", None) if exc is not None else None,
            field,
            suppressed,
            extra={"correlation_id": correlation_id} if correlation_id else None,
        )

    def report_many(self, situation: str, action_types: Sequence[str]) -> None:
        """Count one lost record per entry and log ONE summary ERROR line.

        Not rate-limited: used once per writer stop, where a per-row line
        would flood the log.  Carries per-action-type counts only.
        """
        if not action_types:
            return
        with self._lock:
            self._dropped += len(action_types)
        logger.error(
            "%s: count=%d action_types=%s",
            situation,
            len(action_types),
            dict(sorted(Counter(action_types).items())),
        )


class _AuditBinding:
    """Process wiring: whether this is a server process, and its sink."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.server_process = False
        self.service: Optional["AuditLogService"] = None


_binding = _AuditBinding()
_reporter = _DropReporter()


def mark_server_process() -> None:
    """Declare this process a server: captures now require a bound sink."""
    with _binding.lock:
        _binding.server_process = True


def is_server_process() -> bool:
    """True once :func:`mark_server_process` ran in this process."""
    with _binding.lock:
        return _binding.server_process


def reset_server_process_mark() -> None:
    """Undo :func:`mark_server_process` (test isolation between app builds)."""
    with _binding.lock:
        _binding.server_process = False


def bind_audit_service(svc: "AuditLogService", *, node_id: Optional[str]) -> None:
    """Bind the ONE lifespan-owned audit service and this node's id."""
    if svc is None:
        raise ValueError("bind_audit_service requires an AuditLogService")
    with _binding.lock:
        _binding.service = svc
    set_process_node_id(node_id)


def clear_audit_service() -> None:
    """Unbind at shutdown (after the writer has drained)."""
    with _binding.lock:
        _binding.service = None
    set_process_node_id(None)


def resolve_audit_sink(caller: str) -> Optional["AuditLogService"]:
    """Return the bound service; None only in a never-marked (CLI) process.

    Raises:
        AuditServiceUnresolvable: a server process with nothing bound.
    """
    with _binding.lock:
        server_process = _binding.server_process
        service = _binding.service
    if not server_process:
        return None
    if service is None:
        raise AuditServiceUnresolvable(caller)
    return service


def records_dropped_since_boot() -> int:
    """Audit records this process failed to write since it started."""
    return _reporter.dropped


def audit_node_id() -> Optional[str]:
    """The node id stamped on this process's audit rows (None in solo)."""
    return process_node_id()


def report_drop(
    situation: str, event: AuditEvent, exc: Optional[BaseException] = None
) -> None:
    """Count one lost *event* and log it (used by the service's writer)."""
    _reporter.report(
        situation,
        action_type=event.action_type,
        event_uuid=event.event_uuid,
        correlation_id=event.correlation_id,
        exc=exc,
    )


def report_unwritten_at_stop(events: Sequence[AuditEvent]) -> None:
    """Count every event the stopping writer did not write; ONE ERROR line."""
    _reporter.report_many(WRITER_STOPPED, [event.action_type for event in events])


def _event_loop_owns_this_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _NoSiemDestination:
    """Sentinel: the emitter supplied no explicit SIEM destination."""

    def __repr__(self) -> str:
        return "NO_SIEM_DESTINATION"


# Only SIEM self-report emitters pass an explicit destination: a SiemTarget,
# or None meaning "deliberately not captured: no destination exists".
NO_SIEM_DESTINATION: Any = _NoSiemDestination()


def _deliver(event: AuditEvent, siem_destination: Any = NO_SIEM_DESTINATION) -> None:
    spec = AUDIT_ACTION_CATALOG.get(event.action_type)
    if spec is None:
        _reporter.report(
            EVENT_REJECTED,
            action_type=event.action_type,
            event_uuid=event.event_uuid,
            correlation_id=event.correlation_id,
            field="action_type",
        )
        return
    try:
        sink = resolve_audit_sink("record")
    except AuditServiceUnresolvable as exc:
        report_drop(SERVICE_UNRESOLVABLE, event, exc)
        return
    if sink is None:
        return  # standalone CLI: no store, by design
    _deliver_to(sink, spec.delivery, event, siem_destination)


def _deliver_to(
    sink: "AuditLogService",
    delivery: Delivery,
    event: AuditEvent,
    siem_destination: Any = NO_SIEM_DESTINATION,
) -> None:
    # The QUEUED / on-loop writer path does not carry an explicit SIEM
    # destination: a self-report row reaching it is a counted capture gap.
    if delivery is Delivery.QUEUED:
        sink.enqueue_event(event)
        return
    if _event_loop_owns_this_thread():
        # A missed offload: never block the loop.  The row still reaches the
        # store through the writer thread.
        _reporter.report(
            ON_EVENT_LOOP,
            action_type=event.action_type,
            event_uuid=event.event_uuid,
            correlation_id=event.correlation_id,
            counted=False,
        )
        sink.enqueue_event(event)
        return
    if siem_destination is NO_SIEM_DESTINATION:
        sink.insert_events([event])
        return
    sink.insert_events([event], siem_destinations={event.event_uuid: siem_destination})


def record(event: AuditEvent, siem_destination: Any = NO_SIEM_DESTINATION) -> None:
    """Deliver *event*; never raises (fail-open, counted and logged)."""
    try:
        _deliver(event, siem_destination)
    except Exception as exc:  # noqa: BLE001 - fail-open by owner decision
        report_drop(WRITE_FAILED, event, exc)


def record_legacy(sink: "AuditLogService", event: AuditEvent) -> None:
    """Deliver a pre-existing writer's row through its own started *sink*.

    Used by ``AuditLogService.log`` / ``log_raw`` so their rows follow the
    same catalog delivery (DURABLE or QUEUED) and fail-open policy as
    :func:`record`.  A legacy action type missing from the catalog is
    delivered DURABLE -- the catalog's default -- so no pre-existing writer
    (including the one-shot flat-file migration) loses a row.  Never raises.
    """
    spec = AUDIT_ACTION_CATALOG.get(event.action_type)
    delivery = spec.delivery if spec is not None else Delivery.DURABLE
    try:
        _deliver_to(sink, delivery, event)
    except Exception as exc:  # noqa: BLE001 - fail-open by owner decision
        report_drop(WRITE_FAILED, event, exc)


async def record_async(event: AuditEvent) -> None:
    """:func:`record` from ``async def`` code, off the event loop."""
    await anyio.to_thread.run_sync(record, event)


def _report_invalid(exc: AuditEventInvalid) -> None:
    _reporter.report(
        EVENT_REJECTED, action_type=exc.action_type, exc=exc, field=exc.field
    )


def capture(
    *,
    actor: str,
    action_type: str,
    target_type: str,
    target_id: str,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
    auth_method: Optional[str] = None,
    siem_destination: Any = NO_SIEM_DESTINATION,
) -> None:
    """Build and record a human-actor event; never raises."""
    try:
        event = build_event(
            actor=actor,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=details,
            auth_method=auth_method,
        )
    except AuditEventInvalid as exc:
        _report_invalid(exc)
        return
    record(event, siem_destination)


def capture_system(
    *,
    component: SystemComponent,
    action_type: str,
    target_type: str,
    target_id: str,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
    siem_destination: Any = NO_SIEM_DESTINATION,
) -> None:
    """Build and record a system-component event; never raises."""
    try:
        event = build_system_event(
            component=component,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=details,
        )
    except AuditEventInvalid as exc:
        _report_invalid(exc)
        return
    record(event, siem_destination)


async def capture_async(
    *,
    actor: str,
    action_type: str,
    target_type: str,
    target_id: str,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
    auth_method: Optional[str] = None,
) -> None:
    """:func:`capture` from ``async def`` code, off the event loop."""
    await anyio.to_thread.run_sync(
        functools.partial(
            capture,
            actor=actor,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=details,
            auth_method=auth_method,
        )
    )


async def capture_system_async(
    *,
    component: SystemComponent,
    action_type: str,
    target_type: str,
    target_id: str,
    outcome: str,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    """:func:`capture_system` from ``async def`` code, off the event loop."""
    await anyio.to_thread.run_sync(
        functools.partial(
            capture_system,
            component=component,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=details,
        )
    )
