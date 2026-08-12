"""Tests for ActivityBeacon (Issue #1530).

ActivityBeacon is the in-process, thread-safe primitive that lets worker
threads report fine-grained forward-progress ticks. The parent-side watchdog
(built in a later pass) reads the *oldest in-flight tick's age* to decide
whether a subprocess has genuinely wedged -- never total elapsed time (Bug
#1218 invariant preserved).

These tests use REAL threads and REAL time.monotonic()/time.sleep() -- no
mocking of the beacon itself, per this project's anti-mock rule.
"""

import threading
import time

import pytest

from code_indexer.services.activity_beacon import (
    ActivityBeacon,
    get_activity_beacon,
    set_activity_beacon,
)

# Constants for TestLockNeverSpansWrappedCall.
_LOCK_TEST_LONG_TICK_DURATION = 1.0
_LOCK_TEST_SECOND_THREAD_MAX_DURATION = (
    0.3  # must stay << _LOCK_TEST_LONG_TICK_DURATION
)
_LOCK_TEST_JOIN_TIMEOUT = 5.0


class TestLockNeverSpansWrappedCall:
    """Test (d): the beacon's internal lock must be held only for the
    microseconds needed to mutate the per-thread in-flight dict on
    enter/exit -- never for the duration of the wrapped operation itself.

    Proof: thread A enters a tick whose body sleeps for a long time.
    Concurrently, thread B enters and exits its OWN, unrelated, short tick.
    If the lock spanned thread A's wrapped call, thread B's tick would
    block for (approximately) thread A's whole sleep duration. Instead it
    must complete almost immediately.
    """

    def test_second_thread_tick_does_not_block_on_first_threads_long_running_tick(
        self,
    ) -> None:
        beacon = ActivityBeacon()
        first_thread_tick_entered = threading.Event()

        def long_running_tick() -> None:
            with beacon.tick("long_running_operation"):
                first_thread_tick_entered.set()
                time.sleep(_LOCK_TEST_LONG_TICK_DURATION)

        first_thread = threading.Thread(target=long_running_tick, daemon=True)
        first_thread.start()
        assert first_thread_tick_entered.wait(timeout=_LOCK_TEST_JOIN_TIMEOUT), (
            "first thread never entered its tick"
        )

        second_thread_duration_holder = []

        def short_tick() -> None:
            start = time.monotonic()
            with beacon.tick("short_operation"):
                pass
            second_thread_duration_holder.append(time.monotonic() - start)

        second_thread = threading.Thread(target=short_tick, daemon=True)
        second_thread.start()
        second_thread.join(timeout=_LOCK_TEST_JOIN_TIMEOUT)

        assert second_thread_duration_holder, "second thread's tick never completed"
        second_thread_duration = second_thread_duration_holder[0]
        assert second_thread_duration < _LOCK_TEST_SECOND_THREAD_MAX_DURATION, (
            f"second thread's tick took {second_thread_duration}s -- the "
            f"beacon's lock must not span the first thread's wrapped call "
            f"(which sleeps for {_LOCK_TEST_LONG_TICK_DURATION}s)"
        )

        first_thread.join(
            timeout=_LOCK_TEST_JOIN_TIMEOUT + _LOCK_TEST_LONG_TICK_DURATION
        )


# Test timing constants (all in seconds unless noted).
_WEDGE_JOIN_TIMEOUT = 5.0  # bound on how long the test waits for the wedged
# thread to actually exit once released -- never a bound on the beacon logic.
_HEALTHY_WORKER_TICK_SLEEP = 0.01  # each healthy tick's simulated work time.
_HEALTHY_WORKER_COUNT = 3
_HEALTHY_WORKER_MAX_RUNTIME = 2.0  # deadline bounding the healthy loop below.
_FIRST_OBSERVATION_DELAY = 0.5
_SECOND_OBSERVATION_DELAY = 0.3
_MIN_EXPECTED_WEDGE_AGE = _FIRST_OBSERVATION_DELAY - 0.1

# Constants for TestSlowButAlwaysProgressingNeverAccumulatesStaleness.
_SLOW_PROGRESSOR_TICK_COUNT = 25
_SLOW_PROGRESSOR_TICK_DURATION = 0.02
_SLOW_PROGRESSOR_IDLE_GAP = 0.03


class TestSlowButAlwaysProgressingNeverAccumulatesStaleness:
    """Test (b): a job that ticks in a loop for a long total duration must
    never look stale BETWEEN ticks, however long the total wall-clock run
    is. This is the direct proof of the Bug #1218 invariant this primitive
    must preserve: staleness is "zero forward motion for N seconds", never
    "total elapsed time".
    """

    def test_idle_between_ticks_reads_as_healthy(self) -> None:
        beacon = ActivityBeacon()

        for _ in range(_SLOW_PROGRESSOR_TICK_COUNT):
            with beacon.tick("slow_progressor_step"):
                time.sleep(_SLOW_PROGRESSOR_TICK_DURATION)

            # Idle gap: nothing in flight right now. However many seconds
            # of total wall-clock time have already elapsed across the
            # loop so far, this must read as perfectly healthy.
            assert beacon.oldest_in_flight_age_seconds() is None, (
                "between ticks, nothing is in flight -- staleness must "
                "never accumulate from total elapsed wall-clock time"
            )
            time.sleep(_SLOW_PROGRESSOR_IDLE_GAP)

        # After the entire (deliberately long-running) loop, still healthy.
        assert beacon.oldest_in_flight_age_seconds() is None


class TestOldestInFlightAgeDiscriminatesWedgedThread:
    """Test (a): a single wedged thread must be detected even while other
    threads keep completing ticks in a tight loop.

    A naive design tracking one global `last_activity_time` scalar (bumped
    by ANY thread's tick completion) would report a near-zero age forever
    here, because the healthy threads keep bumping the scalar every few
    milliseconds -- completely masking the permanently wedged thread. That
    is precisely the leading production suspect (one worker holding the
    shared Tantivy writer lock forever while the other workers keep
    finishing in-flight chunks). This test proves the per-thread dict design
    is required: the wedged thread's age must grow unboundedly and become
    observable via `oldest_in_flight_age_seconds()`, independent of how
    often the other threads tick.
    """

    def test_wedged_thread_age_grows_while_others_keep_completing(self) -> None:
        beacon = ActivityBeacon()
        never_set_event = threading.Event()
        stop_healthy_threads = threading.Event()

        def wedged_worker() -> None:
            with beacon.tick("wedged_operation"):
                # Simulates a permanent wedge: blocks until released by the
                # test's finally block, bounded only by this join timeout
                # (never by the beacon itself).
                never_set_event.wait(timeout=_WEDGE_JOIN_TIMEOUT)

        def healthy_worker() -> None:
            # Bounded by BOTH the stop event AND a wall-clock deadline, so
            # this thread can never spin unboundedly even if the test fails
            # to signal stop_healthy_threads for some reason.
            deadline = time.monotonic() + _HEALTHY_WORKER_MAX_RUNTIME
            while not stop_healthy_threads.is_set() and time.monotonic() < deadline:
                with beacon.tick("healthy_operation"):
                    time.sleep(_HEALTHY_WORKER_TICK_SLEEP)

        wedged_thread = threading.Thread(target=wedged_worker, daemon=True)
        healthy_threads = [
            threading.Thread(target=healthy_worker, daemon=True)
            for _ in range(_HEALTHY_WORKER_COUNT)
        ]

        wedged_thread.start()
        for t in healthy_threads:
            t.start()

        try:
            # Let the wedge age past a point where "any activity anywhere"
            # would look perpetually fresh under a global-scalar design.
            time.sleep(_FIRST_OBSERVATION_DELAY)
            age = beacon.oldest_in_flight_age_seconds()

            assert age is not None, (
                "oldest_in_flight_age_seconds() must report the wedged "
                "thread's age even while healthy threads keep ticking"
            )
            # A global-last-activity-time design would have returned an age
            # near _HEALTHY_WORKER_TICK_SLEEP here (whatever the healthy
            # threads' loop interval is) -- this assertion is only true
            # because the age is derived from the OLDEST in-flight entry
            # (the wedge), not "time since anything, anywhere, last
            # completed".
            assert age >= _MIN_EXPECTED_WEDGE_AGE, (
                f"expected the wedged thread's age (~{_FIRST_OBSERVATION_DELAY}s) "
                f"to dominate, got {age}s -- a global scalar would have "
                f"reported ~{_HEALTHY_WORKER_TICK_SLEEP}s"
            )

            # Prove the wedge keeps aging while healthy threads are still
            # actively completing work concurrently.
            time.sleep(_SECOND_OBSERVATION_DELAY)
            age2 = beacon.oldest_in_flight_age_seconds()
            assert age2 is not None
            assert age2 > age, "wedged thread's age must keep growing"
        finally:
            never_set_event.set()
            stop_healthy_threads.set()
            wedged_thread.join(timeout=_WEDGE_JOIN_TIMEOUT)
            for t in healthy_threads:
                t.join(timeout=_WEDGE_JOIN_TIMEOUT)


# Constants for TestStalenessSignalIsObservable.
_OBSERVABLE_STUCK_LABEL = "stuck_operation"
_OBSERVABLE_FIRST_WAIT = 0.2
_OBSERVABLE_SECOND_WAIT = 0.2
_OBSERVABLE_JOIN_TIMEOUT = 5.0
_OBSERVABLE_AGREEMENT_TOLERANCE_SECONDS = 0.05


class TestStalenessSignalIsObservable:
    """Test (c): the staleness signal must be genuinely inspectable by a
    caller (e.g. a future parent-side watchdog), not just an internal,
    opaque state. `snapshot()` must expose enough detail (which label is
    stuck, and its age) for production logs to be useful -- and that age
    must agree with `oldest_in_flight_age_seconds()`.

    This test lives at the beacon level only: the full "kill is logged and
    surfaces as a real job failure" assertion belongs to the parent-side
    watchdog integration (a later pass), not to this primitive.
    """

    def test_snapshot_surfaces_stuck_label_and_growing_age(self) -> None:
        beacon = ActivityBeacon()
        entered = threading.Event()
        release = threading.Event()

        def stuck_worker() -> None:
            with beacon.tick(_OBSERVABLE_STUCK_LABEL):
                entered.set()
                release.wait(timeout=_OBSERVABLE_JOIN_TIMEOUT)

        worker_thread = threading.Thread(target=stuck_worker, daemon=True)
        worker_thread.start()
        try:
            assert entered.wait(timeout=_OBSERVABLE_JOIN_TIMEOUT)

            time.sleep(_OBSERVABLE_FIRST_WAIT)
            snapshot_1 = beacon.snapshot()
            assert snapshot_1["in_flight_count"] == 1
            stuck_entry_1 = snapshot_1["in_flight"][0]
            assert stuck_entry_1["label"] == _OBSERVABLE_STUCK_LABEL
            assert stuck_entry_1["age_seconds"] >= _OBSERVABLE_FIRST_WAIT - 0.1
            # snapshot() and oldest_in_flight_age_seconds() are two
            # INDEPENDENT calls, each capturing its own fresh
            # time.monotonic() -- exact equality between them would be
            # flaky by design. A small tolerance still proves genuine
            # agreement between the two query paths.
            age_via_direct_call = beacon.oldest_in_flight_age_seconds()
            assert age_via_direct_call is not None
            assert (
                abs(snapshot_1["oldest_in_flight_age_seconds"] - age_via_direct_call)
                < _OBSERVABLE_AGREEMENT_TOLERANCE_SECONDS
            )
            # This equality (against the snapshot's OWN per-entry data) is
            # exact and non-flaky: both sides are derived from the same
            # snapshot_1 dict, no second time.monotonic() call involved.
            assert snapshot_1["oldest_in_flight_age_seconds"] == max(
                entry["age_seconds"] for entry in snapshot_1["in_flight"]
            ), "oldest_in_flight_age_seconds must equal the max per-entry age"

            time.sleep(_OBSERVABLE_SECOND_WAIT)
            snapshot_2 = beacon.snapshot()
            stuck_entry_2 = snapshot_2["in_flight"][0]
            assert stuck_entry_2["age_seconds"] > stuck_entry_1["age_seconds"], (
                "the observable age must keep growing for a genuinely stuck operation"
            )
        finally:
            release.set()
            worker_thread.join(timeout=_OBSERVABLE_JOIN_TIMEOUT)


# Constants for TestProviderDelayIsHealthyUntilDeadline.
_PROVIDER_DELAY_WINDOW_SECONDS = 0.3
_PROVIDER_DELAY_PAST_DEADLINE_WAIT = 0.15
_PROVIDER_DELAY_MAX_HEALTHY_AGE = 0.001
_PROVIDER_DELAY_JOIN_TIMEOUT = 5.0


class TestProviderDelayIsHealthyUntilDeadline:
    """A legitimate provider-directed delay (VoyageAI/Cohere `Retry-After`,
    up to ~300s) must read as healthy for its entire duration, however long
    that is -- and once the deadline passes, age must be measured FROM the
    deadline, not from when the delay context was entered (a caller-side
    staleness threshold then naturally measures "how long overdue", never
    "how long the provider asked us to wait").
    """

    def test_delay_reads_healthy_before_deadline_then_ages_from_deadline(self) -> None:
        beacon = ActivityBeacon()
        entered = threading.Event()
        release = threading.Event()
        deadline_holder = []

        def delayed_worker() -> None:
            deadline = time.monotonic() + _PROVIDER_DELAY_WINDOW_SECONDS
            deadline_holder.append(deadline)
            with beacon.waiting_on_provider_delay(until=deadline):
                entered.set()
                release.wait(timeout=_PROVIDER_DELAY_JOIN_TIMEOUT)

        worker_thread = threading.Thread(target=delayed_worker, daemon=True)
        worker_thread.start()
        try:
            assert entered.wait(timeout=_PROVIDER_DELAY_JOIN_TIMEOUT)

            # Still well within the delay window: must read as healthy
            # (age 0), regardless of how long the window itself is.
            age_before_deadline = beacon.oldest_in_flight_age_seconds()
            assert age_before_deadline is not None
            assert age_before_deadline <= _PROVIDER_DELAY_MAX_HEALTHY_AGE, (
                f"expected ~0 age while inside the provider delay window, "
                f"got {age_before_deadline}"
            )

            # Wait past the deadline, then measure age FROM the deadline.
            deadline = deadline_holder[0]
            remaining_until_deadline = max(0.0, deadline - time.monotonic())
            time.sleep(remaining_until_deadline + _PROVIDER_DELAY_PAST_DEADLINE_WAIT)

            age_after_deadline = beacon.oldest_in_flight_age_seconds()
            assert age_after_deadline is not None
            assert age_after_deadline >= _PROVIDER_DELAY_PAST_DEADLINE_WAIT - 0.05
            # Must NOT be measured from the original tick start -- it must
            # be small (roughly _PROVIDER_DELAY_PAST_DEADLINE_WAIT), never
            # roughly the full window+past-deadline elapsed time.
            assert age_after_deadline < _PROVIDER_DELAY_WINDOW_SECONDS, (
                f"age {age_after_deadline} looks like it was measured from "
                f"the original delay start, not from the deadline"
            )
        finally:
            release.set()
            worker_thread.join(timeout=_PROVIDER_DELAY_JOIN_TIMEOUT)


# Constants for TestNestedTicksDoNotCorruptStack.
_NESTED_PRE_INNER_WAIT = 0.1
_NESTED_INNER_TICK_DURATION = 0.05
_NESTED_POST_INNER_WAIT = 0.1
_NESTED_TIMING_TOLERANCE = 0.05


class TestNestedTicksDoNotCorruptStack:
    """Regression test for a real defect found during review: a naive
    single-slot-per-thread design let a nested (inner) tick's __exit__
    delete the outer tick's still-open entry (either via a blind
    dict-overwrite, or via `list.remove()` matching by dataclass VALUE
    equality instead of object identity). Both were fixed by (1) a
    per-thread STACK rather than a single slot, and (2) identity-based
    removal (`is`, never `==`).

    Proof: the outer tick's age must keep growing continuously across the
    entire nested sequence -- it must never reset to a smaller value or
    disappear (None) merely because an inner tick opened and closed inside
    it.
    """

    def test_inner_tick_exit_does_not_remove_outer_ticks_entry(self) -> None:
        beacon = ActivityBeacon()

        with beacon.tick("outer_operation"):
            time.sleep(_NESTED_PRE_INNER_WAIT)
            age_before_inner = beacon.oldest_in_flight_age_seconds()
            assert age_before_inner is not None
            assert age_before_inner >= _NESTED_PRE_INNER_WAIT - _NESTED_TIMING_TOLERANCE

            with beacon.tick("inner_operation"):
                time.sleep(_NESTED_INNER_TICK_DURATION)
                age_during_inner = beacon.oldest_in_flight_age_seconds()
                # The OUTER entry dominates (it started earlier), so the
                # oldest age must reflect the outer tick's elapsed time,
                # not the inner one's.
                assert age_during_inner is not None
                assert age_during_inner >= age_before_inner

            # Inner tick has exited -- the outer entry must still be
            # present and must NOT have reset/disappeared.
            age_after_inner_exit = beacon.oldest_in_flight_age_seconds()
            assert age_after_inner_exit is not None, (
                "the outer tick's entry was incorrectly removed by the "
                "inner tick's __exit__"
            )
            assert age_after_inner_exit >= age_during_inner, (
                "the outer entry's age must keep growing monotonically "
                "across the nested inner tick's full lifecycle"
            )

            time.sleep(_NESTED_POST_INNER_WAIT)
            age_before_outer_exit = beacon.oldest_in_flight_age_seconds()
            assert age_before_outer_exit is not None
            assert age_before_outer_exit > age_after_inner_exit

        # Outer tick has now exited too -- nothing left in flight.
        assert beacon.oldest_in_flight_age_seconds() is None


class TestInputValidationAndSingletonAccessor:
    """Validation on the two public entry points that accept caller-
    supplied values, plus the module-level singleton DI-override accessor
    used by Priority 5 (CLI-side beacon activation) and its tests.
    """

    def test_tick_rejects_empty_or_non_string_label(self) -> None:
        beacon = ActivityBeacon()
        with pytest.raises(ValueError):
            beacon.tick("")
        with pytest.raises(ValueError):
            beacon.tick(123)  # type: ignore[arg-type]

    def test_waiting_on_provider_delay_rejects_non_finite_deadline(self) -> None:
        beacon = ActivityBeacon()
        with pytest.raises(ValueError):
            beacon.waiting_on_provider_delay(until=float("nan"))
        with pytest.raises(ValueError):
            beacon.waiting_on_provider_delay(until=float("inf"))
        with pytest.raises(ValueError):
            beacon.waiting_on_provider_delay(until=True)  # type: ignore[arg-type]

    def test_singleton_accessor_get_set_roundtrip(self) -> None:
        try:
            first = get_activity_beacon()
            second = get_activity_beacon()
            assert first is second, "singleton must return the same instance"

            custom = ActivityBeacon()
            set_activity_beacon(custom)
            assert get_activity_beacon() is custom
        finally:
            set_activity_beacon(None)


# Additional constants for the nested-provider-delay-inside-tick test.
_NESTED_DELAY_PRE_WAIT = 0.05
_NESTED_DELAY_WINDOW_SECONDS = 0.2
_NESTED_DELAY_MAX_HEALTHY_AGE = 0.001


class TestProviderDelayNestedInsideActiveTick:
    """Realistic usage from the issue's own design: each HTTP attempt is
    its own `tick()`, and ONLY when a valid `Retry-After` is received does
    the SAME thread mark that SAME already-open entry as delayed (rather
    than creating a second, standalone entry). This exercises the
    `_ProviderDelayContext.__enter__` "stack is non-empty" branch and the
    `__exit__` "restore previous provider_delay_until" branch -- distinct
    from `TestProviderDelayIsHealthyUntilDeadline`, which covers the
    standalone (no enclosing tick) usage.
    """

    def test_provider_delay_nested_inside_active_tick_restores_previous_state(
        self,
    ) -> None:
        beacon = ActivityBeacon()

        with beacon.tick("embedding_http_attempt"):
            time.sleep(_NESTED_DELAY_PRE_WAIT)
            age_before_delay = beacon.oldest_in_flight_age_seconds()
            assert age_before_delay is not None
            assert age_before_delay >= _NESTED_DELAY_PRE_WAIT - _NESTED_TIMING_TOLERANCE

            # Before entering the delay: the enclosing tick's entry has no
            # provider-delay marker at all.
            entry_before = beacon.snapshot()["in_flight"][0]
            assert entry_before["waiting_on_provider_delay_until"] is None

            deadline = time.monotonic() + _NESTED_DELAY_WINDOW_SECONDS
            with beacon.waiting_on_provider_delay(until=deadline):
                snap_during = beacon.snapshot()
                # Still one single in-flight entry (the SAME tick entry,
                # now marked delayed) -- not a second standalone one.
                assert snap_during["in_flight_count"] == 1
                entry_during = snap_during["in_flight"][0]
                assert entry_during["waiting_on_provider_delay_until"] == deadline

                age_during_delay = beacon.oldest_in_flight_age_seconds()
                assert age_during_delay is not None
                assert age_during_delay <= _NESTED_DELAY_MAX_HEALTHY_AGE, (
                    "the enclosing tick's entry must read healthy while "
                    "marked as waiting on a provider delay"
                )

            # Delay context exited (before its deadline, in this test) --
            # the entry's provider_delay_until must be restored to None
            # DIRECTLY (checked via the field, not merely inferred through
            # age comparisons), so it resumes ordinary start_time-based
            # aging immediately.
            snap_after = beacon.snapshot()
            assert snap_after["in_flight_count"] == 1
            entry_after = snap_after["in_flight"][0]
            assert entry_after["waiting_on_provider_delay_until"] is None

            age_after_delay_exit = beacon.oldest_in_flight_age_seconds()
            assert age_after_delay_exit is not None
            assert age_after_delay_exit >= age_before_delay, (
                "after the provider-delay context exits, aging must "
                "resume from the ORIGINAL tick start_time, not reset"
            )

        assert beacon.oldest_in_flight_age_seconds() is None
