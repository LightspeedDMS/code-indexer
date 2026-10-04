"""Bug #2022: the cidx-meta debouncer's retry pacing after a generic failure
(store or job tracker unavailable) -- doubling, capped, reset by a success,
and never rescheduled after shutdown.

Timers are never waited on: the debounce interval is long, and each test
runs the pending timer's callback directly, then reads the interval the
next retry was scheduled at. Only the refresh scheduler's trigger is
replaced, by a fake that plays a fixed script of outcomes.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Union

from code_indexer.global_repos.meta_description_hook import (
    _MAX_RETRY_INTERVAL_SECONDS,
    CidxMetaRefreshDebouncer,
)

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

    def trigger_refresh_for_repo(self, alias_name: str) -> str:
        assert alias_name == META_ALIAS
        if self.before_outcome is not None:
            self.before_outcome()
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _store_down() -> Exception:
    return RuntimeError("metadata store unavailable")


def _expire_now(debouncer: CidxMetaRefreshDebouncer) -> None:
    """Run the pending timer's callback now instead of waiting for it."""
    with debouncer._lock:
        pending = debouncer._timer
    assert pending is not None, "no timer pending"
    pending.cancel()
    debouncer._on_timer_expired()


def _next_retry_interval(debouncer: CidxMetaRefreshDebouncer) -> float:
    with debouncer._lock:
        pending = debouncer._timer
    assert pending is not None, "the refresh is owed but no retry is pending"
    return float(pending.interval)


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
