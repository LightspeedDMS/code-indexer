"""Bug #2022: the cidx-meta debouncer's retry pacing after a generic failure
(store or job tracker unavailable) -- doubling, capped, reset by a success,
and never rescheduled after shutdown.

Timers are never waited on: the debounce interval is long, and each test
runs the pending timer's callback directly, then reads the interval the
next retry was scheduled at. Only the refresh scheduler's trigger is
replaced, by a fake that plays a fixed script of outcomes.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional, Sequence, Union

from code_indexer.global_repos.meta_description_hook import (
    _MAX_RETRY_INTERVAL_SECONDS,
    CidxMetaRefreshDebouncer,
)
from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError

META_ALIAS = "cidx-meta-global"
DEBOUNCE_SECONDS = 30.0  # never elapses during a test
FIRST_RETRY_SECONDS = 2 * DEBOUNCE_SECONDS  # after one failure
MANY_FAILURES = 12  # well past the cap: 30 s * 2**12 >> 900 s

Outcome = Union[str, Exception]


class _ScriptedScheduler:
    """The refresh scheduler's trigger, playing a fixed script of outcomes
    (a job id is returned, an exception is raised)."""

    def __init__(self, outcomes: Sequence[Outcome]) -> None:
        self._outcomes: List[Outcome] = list(outcomes)
        self.before_outcome: Optional[Callable[[], None]] = None
        self.calls = 0

    def trigger_refresh_for_repo(self, alias_name: str) -> str:
        assert alias_name == META_ALIAS
        self.calls += 1
        if self.before_outcome is not None:
            self.before_outcome()
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _store_down() -> Exception:
    return RuntimeError("metadata store unavailable")


def _pending_timer(debouncer: CidxMetaRefreshDebouncer) -> threading.Timer:
    with debouncer._lock:
        pending = debouncer._timer
    assert pending is not None, "the refresh is owed but no timer is pending"
    return pending


def _run_callback(timer: threading.Timer) -> None:
    """Run a timer's callback exactly as Timer.run would once its wait ends."""
    timer.function(*timer.args, **timer.kwargs)


def _expire_now(debouncer: CidxMetaRefreshDebouncer) -> None:
    """Run the pending timer's callback now instead of waiting for it."""
    pending = _pending_timer(debouncer)
    pending.cancel()
    _run_callback(pending)


def _next_retry_interval(debouncer: CidxMetaRefreshDebouncer) -> float:
    return float(_pending_timer(debouncer).interval)


def test_retry_interval_is_capped_after_many_failures() -> None:
    scheduler = _ScriptedScheduler([_store_down()] * MANY_FAILURES)
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    try:
        debouncer.signal_dirty()
        intervals = []
        for _ in range(MANY_FAILURES):
            _expire_now(debouncer)
            intervals.append(_next_retry_interval(debouncer))

        assert intervals[0] == FIRST_RETRY_SECONDS
        assert max(intervals) <= _MAX_RETRY_INTERVAL_SECONDS, intervals
        assert intervals[-1] == _MAX_RETRY_INTERVAL_SECONDS, intervals
        assert intervals == sorted(intervals), "retry pacing is not monotonic"
        assert debouncer._dirty is True, "the refresh stopped being owed"
    finally:
        debouncer.shutdown()


def test_a_success_resets_the_retry_interval() -> None:
    scheduler = _ScriptedScheduler([_store_down()] * 4 + ["job-1", _store_down()])
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    try:
        debouncer.signal_dirty()
        for _ in range(4):
            _expire_now(debouncer)
        assert _next_retry_interval(debouncer) > FIRST_RETRY_SECONDS

        _expire_now(debouncer)  # the outage clears: submitted
        assert debouncer._dirty is False
        assert debouncer._timer is None

        debouncer.signal_dirty()  # a later write
        _expire_now(debouncer)  # fails once

        assert _next_retry_interval(debouncer) == FIRST_RETRY_SECONDS, (
            "a past outage still slows the retries of a new one"
        )
    finally:
        debouncer.shutdown()


def test_a_failure_completing_after_shutdown_schedules_no_retry() -> None:
    scheduler = _ScriptedScheduler([_store_down()])
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    scheduler.before_outcome = debouncer.shutdown  # shutdown while in flight
    debouncer.signal_dirty()

    _expire_now(debouncer)

    assert debouncer._timer is None, "a retry was scheduled after shutdown"


def test_a_cancelled_callback_never_acts_for_its_replacement() -> None:
    scheduler = _ScriptedScheduler(["job-1"])
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    try:
        debouncer.signal_dirty()
        stale = _pending_timer(debouncer)
        debouncer.signal_dirty()  # a later write: cancels it, installs a replacement
        replacement = _pending_timer(debouncer)
        assert replacement is not stale

        # The cancelled timer had already passed Timer.run's cancellation
        # check, so its callback still runs.
        _run_callback(stale)

        assert scheduler.calls == 0, "a cancelled timer submitted the refresh"
        assert _pending_timer(debouncer) is replacement, "the replacement was orphaned"
    finally:
        debouncer.shutdown()
    assert replacement.finished.is_set(), "shutdown could not cancel the replacement"


def _deferred() -> Exception:
    return RefreshDeferredError(META_ALIAS, time.time() + DEBOUNCE_SECONDS)


def test_a_deferral_settles_only_the_writes_before_it() -> None:
    scheduler = _ScriptedScheduler([_deferred(), _deferred()])
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    try:
        debouncer.signal_dirty()
        scheduler.before_outcome = debouncer.signal_dirty  # a write meanwhile
        _expire_now(debouncer)  # deferred durably, but after that write

        assert debouncer._dirty is True, "a deferral dropped a later write"
        _pending_timer(debouncer)

        scheduler.before_outcome = None
        _expire_now(debouncer)  # a plain deferral

        assert debouncer._dirty is False
        assert debouncer._timer is None, "a durable deferral is retried here"
    finally:
        debouncer.shutdown()


def test_a_failed_retry_never_replaces_a_newer_writes_timer() -> None:
    scheduler = _ScriptedScheduler([_store_down()])
    debouncer = CidxMetaRefreshDebouncer(scheduler, debounce_seconds=DEBOUNCE_SECONDS)
    writes_timers: List[threading.Timer] = []

    def _write_meanwhile() -> None:
        debouncer.signal_dirty()
        writes_timers.append(_pending_timer(debouncer))

    try:
        debouncer.signal_dirty()
        scheduler.before_outcome = _write_meanwhile
        _expire_now(debouncer)  # fails after that write

        assert _pending_timer(debouncer) is writes_timers[0], (
            "the failure's retry replaced the newer write's timer"
        )
        assert _next_retry_interval(debouncer) == DEBOUNCE_SECONDS
    finally:
        debouncer.shutdown()
    assert writes_timers[0].finished.is_set(), "shutdown could not cancel it"
