"""ActivityBeacon: fine-grained, per-thread forward-progress instrumentation.

Issue #1530: production hit an indexing-subprocess deadlock (a `cidx index
--progress-json` child hanging indefinitely at 0% progress). This module is
the detect half of "detect-and-mitigate": a small, thread-safe, in-process
primitive that worker threads use to report ticks around every operation
that should normally complete within seconds. A parent-side watchdog (built
on top of this primitive, in `progress_subprocess_runner.py`) reads the
resulting staleness signal to decide whether a subprocess has genuinely
stopped making forward progress.

Design constraint, non-negotiable: there is NO single global
`last_activity_time` scalar. If the beacon tracked one shared timestamp
bumped by *any* thread's tick, a lone healthy worker would mask a different,
permanently wedged thread forever (the leading production suspect: one
worker holding a shared lock forever while the others keep finishing
in-flight work). Instead, every in-flight tick is recorded per-thread, and
the staleness signal is the age of the OLDEST currently in-flight operation,
computed by scanning the dict at query time -- never a single timestamp.

Per-thread state is a STACK, not a single slot: nested ticks are a real
possibility (e.g. a file-processing tick wrapping an inner embedding-batch
tick), and a single-slot design would let the inner tick's exit silently
delete the outer tick's still-open entry. Because a stack's earlier
(outer) entries always have an earlier start_time than later (inner)
entries, taking the max age across ALL entries (flattened over all threads)
automatically surfaces the correct "how long has this thread's outermost
still-open operation been running" signal with no extra bookkeeping.

Bug #1218 invariant: this primitive measures ZERO forward motion for N
seconds, never total elapsed time. A thread that ticks quickly and
repeatedly for hours never accumulates staleness -- between ticks, nothing
is in flight, so it contributes nothing to the oldest-age computation.

Provider-directed delays (e.g. a VoyageAI/Cohere 429 `Retry-After` sleep of
up to ~300s) are an explicit state, not silence: `waiting_on_provider_delay`
marks the current thread's innermost in-flight entry as healthy up to the
promised deadline, regardless of how far in the future that deadline is.
Once the deadline passes, the age starts accruing from the deadline itself
(not from the original tick start), so a caller-side staleness threshold
naturally measures "how long overdue" rather than "how long since the
provider call began".
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(eq=False)
class _InFlightEntry:
    """One in-flight tick (or provider-delay marker) on a thread's stack.

    `eq=False`: entries must be removed from a thread's stack by IDENTITY,
    never by field-value equality (two ticks with the same label started
    close enough together could otherwise compare equal and remove the
    wrong one). `eq=False` makes this class use the default `object`
    identity-based equality/hash instead of a dataclass-generated
    value-based one.

    `provider_delay_until` is None for an ordinary tick. When set (via
    `waiting_on_provider_delay`), the entry is reported as healthy (age 0)
    until that monotonic deadline, then ages from the deadline onward.
    """

    label: str
    start_time: float
    provider_delay_until: Optional[float] = None


def _effective_age(
    start_time: float, provider_delay_until: Optional[float], now: float
) -> float:
    """Compute one entry's staleness age at time `now` (monotonic clock).

    Takes plain field values (not an `_InFlightEntry` reference) so callers
    can extract an immutable snapshot of the fields while holding the lock,
    then compute ages afterward without racing a concurrent mutation of a
    live entry (e.g. `waiting_on_provider_delay` updating
    `provider_delay_until` on another thread).

    Clamped to 0.0 as a defensive floor: callers always capture `now` after
    reading fields out from under the lock, so this should never go
    negative in practice, but a clock/ordering edge case must never surface
    as a nonsensical negative "staleness".
    """
    if provider_delay_until is not None:
        if now < provider_delay_until:
            return 0.0
        return max(0.0, now - provider_delay_until)
    return max(0.0, now - start_time)


def _validate_label(label: str) -> None:
    if not isinstance(label, str) or not label:
        raise ValueError(
            f"ActivityBeacon tick label must be a non-empty str, got {label!r}"
        )


def _validate_deadline(until: float) -> None:
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        raise ValueError(
            f"provider delay deadline must be a real number, got {until!r}"
        )
    if math.isnan(until) or math.isinf(until):
        raise ValueError(f"provider delay deadline must be finite, got {until!r}")


def _remove_by_identity(stack: List[_InFlightEntry], entry: _InFlightEntry) -> None:
    """Remove `entry` from `stack` by object identity, never by equality.

    Caller MUST hold the beacon's lock. A missing entry is tolerated
    (no-op) rather than raising -- defensive against a caller invoking
    __exit__ twice, which should never happen but must never crash.
    """
    for index, candidate in enumerate(stack):
        if candidate is entry:
            del stack[index]
            return


class ActivityBeacon:
    """Thread-safe, in-process forward-progress tracker.

    Real threads only, real `time.monotonic()` -- no mocking of this class
    itself is meaningful, per this project's anti-mock testing rule.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight: Dict[int, List[_InFlightEntry]] = {}

    def tick(self, label: str) -> "_TickContext":
        """Context manager: pushes an in-flight entry onto the calling
        thread's stack for the duration of the wrapped block, popping it
        on exit (success or exception).

        The lock is held only for the microseconds needed to mutate the
        stack on enter/exit -- never spanning the wrapped body.
        """
        _validate_label(label)
        return _TickContext(self, label)

    def waiting_on_provider_delay(self, until: float) -> "_ProviderDelayContext":
        """Context manager: marks the calling thread's innermost in-flight
        entry (creating a standalone one if the stack is empty) as
        legitimately waiting on a provider-directed delay until the given
        monotonic deadline (`time.monotonic()`-comparable).

        While `time.monotonic() < until`, this thread contributes 0 to the
        oldest-in-flight-age computation, however long `until - now` is.
        """
        _validate_deadline(until)
        return _ProviderDelayContext(self, until)

    def oldest_in_flight_age_seconds(self) -> Optional[float]:
        """Age (seconds) of the longest-running currently in-flight entry.

        Returns None when nothing is in flight anywhere -- an idle beacon
        between operations is healthy, not stale, regardless of total
        elapsed wall-clock time (Bug #1218 invariant).
        """
        field_snapshots = self._snapshot_fields()
        if not field_snapshots:
            return None
        now = time.monotonic()
        return max(
            _effective_age(start_time, provider_delay_until, now)
            for _tid, _label, start_time, provider_delay_until in field_snapshots
        )

    def snapshot(self) -> dict:
        """Serializable snapshot of current in-flight state.

        Intended for a heartbeat-file writer (Priority 2) to persist
        alongside process identity (pid, start time) -- this method reports
        only the beacon's own in-flight state, not process metadata.
        """
        field_snapshots = self._snapshot_fields()
        now = time.monotonic()
        ages: List[float] = [
            _effective_age(start_time, provider_delay_until, now)
            for _tid, _label, start_time, provider_delay_until in field_snapshots
        ]
        in_flight = [
            {
                "thread_id": tid,
                "label": label,
                "age_seconds": age,
                "waiting_on_provider_delay_until": provider_delay_until,
            }
            for (tid, label, _start_time, provider_delay_until), age in zip(
                field_snapshots, ages
            )
        ]
        return {
            "oldest_in_flight_age_seconds": max(ages) if ages else None,
            "in_flight_count": len(in_flight),
            "in_flight": in_flight,
        }

    def _snapshot_fields(
        self,
    ) -> List[Tuple[int, str, float, Optional[float]]]:
        """Extract an immutable snapshot of every in-flight entry's fields
        while holding the lock -- so age computation afterward never races
        a concurrent mutation of a live `_InFlightEntry` (e.g. a
        `waiting_on_provider_delay` context updating `provider_delay_until`
        on a different thread at the same moment).
        """
        with self._lock:
            return [
                (tid, entry.label, entry.start_time, entry.provider_delay_until)
                for tid, stack in self._in_flight.items()
                for entry in stack
            ]


class _TickContext:
    """Context manager returned by `ActivityBeacon.tick()`.

    Pushes a fresh entry onto the calling thread's stack on enter; removes
    that SAME entry (by object identity, never by equality) on exit, so a
    nested tick's exit can never remove an outer tick's still-open entry.
    """

    def __init__(self, beacon: ActivityBeacon, label: str) -> None:
        self._beacon = beacon
        self._label = label
        self._tid: Optional[int] = None
        self._entry: Optional[_InFlightEntry] = None

    def __enter__(self) -> "_TickContext":
        self._tid = threading.get_ident()
        self._entry = _InFlightEntry(label=self._label, start_time=time.monotonic())
        with self._beacon._lock:
            self._beacon._in_flight.setdefault(self._tid, []).append(self._entry)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._tid is not None and self._entry is not None
        with self._beacon._lock:
            stack = self._beacon._in_flight.get(self._tid)
            if stack is not None:
                _remove_by_identity(stack, self._entry)
                if not stack:
                    del self._beacon._in_flight[self._tid]


class _ProviderDelayContext:
    """Context manager returned by `ActivityBeacon.waiting_on_provider_delay()`.

    Marks the calling thread's innermost still-open entry (top of stack) as
    delayed. If the stack is empty, it pushes its own standalone entry
    instead -- and on exit, removes exactly that entry it created (by
    object identity, never by equality) rather than an unrelated entry a
    concurrently-nested tick might have pushed.
    """

    def __init__(self, beacon: ActivityBeacon, until: float) -> None:
        self._beacon = beacon
        self._until = until
        self._tid: Optional[int] = None
        self._entry: Optional[_InFlightEntry] = None
        self._created_own_entry = False
        self._previous_until: Optional[float] = None

    def __enter__(self) -> "_ProviderDelayContext":
        self._tid = threading.get_ident()
        with self._beacon._lock:
            stack = self._beacon._in_flight.setdefault(self._tid, [])
            if stack:
                self._entry = stack[-1]
                self._created_own_entry = False
            else:
                self._entry = _InFlightEntry(
                    label="provider_delay", start_time=time.monotonic()
                )
                stack.append(self._entry)
                self._created_own_entry = True
            self._previous_until = self._entry.provider_delay_until
            self._entry.provider_delay_until = self._until
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        assert self._tid is not None and self._entry is not None
        with self._beacon._lock:
            if self._created_own_entry:
                stack = self._beacon._in_flight.get(self._tid)
                if stack is not None:
                    _remove_by_identity(stack, self._entry)
                    if not stack:
                        del self._beacon._in_flight[self._tid]
            else:
                self._entry.provider_delay_until = self._previous_until


_default_beacon_lock = threading.Lock()
_default_beacon: Optional[ActivityBeacon] = None


def get_activity_beacon() -> ActivityBeacon:
    """Return the process-wide singleton ActivityBeacon, creating it lazily.

    Matches this codebase's existing pattern for shared per-process state
    (e.g. lazily-resolved backend registries) -- callers never need an
    instance threaded through every constructor.
    """
    global _default_beacon
    with _default_beacon_lock:
        if _default_beacon is None:
            _default_beacon = ActivityBeacon()
        return _default_beacon


def set_activity_beacon(beacon: Optional[ActivityBeacon]) -> None:
    """DI override for tests: force a specific instance (or None to reset)."""
    global _default_beacon
    with _default_beacon_lock:
        _default_beacon = beacon
