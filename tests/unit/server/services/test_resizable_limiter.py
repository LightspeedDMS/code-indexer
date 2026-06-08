"""Unit tests for ResizableLimiter (Story #1079 Phase B).

ResizableLimiter is a lock+condition concurrency limiter that REPLACES
threading.BoundedSemaphore. Unlike a BoundedSemaphore it can grow/shrink its
limit at runtime (set_limit) and is the single source of truth for per-lane
in_flight / high_water telemetry.

All tests use real threads — NO mocks.
"""

import threading
import time
from typing import List

from code_indexer.server.services.resizable_limiter import (
    K_MAX,
    K_MIN,
    ResizableLimiter,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestResizableLimiterBasic:
    def test_acquire_release_happy_path(self):
        lim = ResizableLimiter(initial=8)
        assert lim.acquire(timeout=1.0) is True
        assert lim.in_flight == 1
        lim.release()
        assert lim.in_flight == 0

    def test_high_water_tracks_peak(self):
        lim = ResizableLimiter(initial=8)
        assert lim.acquire(timeout=1.0) is True
        assert lim.acquire(timeout=1.0) is True
        assert lim.in_flight == 2
        assert lim.high_water == 2
        lim.release()
        # high_water is a PEAK — it does not decrease when in_flight does
        assert lim.in_flight == 1
        assert lim.high_water == 2
        lim.release()
        assert lim.high_water == 2

    def test_limit_property_reflects_initial_clamped(self):
        # initial below floor clamps up to K_MIN
        assert ResizableLimiter(initial=1).limit == K_MIN
        # initial above ceiling clamps down to K_MAX
        assert ResizableLimiter(initial=999).limit == K_MAX
        # in-range stays
        assert ResizableLimiter(initial=20).limit == 20


# ---------------------------------------------------------------------------
# Blocking / timeout
# ---------------------------------------------------------------------------


class TestResizableLimiterBlocking:
    def test_acquire_blocks_at_limit_release_wakes(self):
        lim = ResizableLimiter(initial=K_MIN)  # K_MIN == 8
        # Saturate to the limit
        for _ in range(K_MIN):
            assert lim.acquire(timeout=1.0) is True
        assert lim.in_flight == K_MIN

        woke = threading.Event()

        def waiter():
            if lim.acquire(timeout=5.0):
                woke.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        # Waiter should be parked, not yet acquired
        time.sleep(0.1)
        assert not woke.is_set()

        # Release one slot -> waiter must wake
        lim.release()
        t.join(timeout=5.0)
        assert woke.is_set(), "release() must wake a parked acquirer"

    def test_acquire_timeout_returns_false(self):
        lim = ResizableLimiter(initial=K_MIN)
        for _ in range(K_MIN):
            assert lim.acquire(timeout=1.0) is True
        # No slot free -> timeout returns False (NOT a hang, NOT an exception)
        t0 = time.monotonic()
        assert lim.acquire(timeout=0.1) is False
        assert time.monotonic() - t0 >= 0.09


# ---------------------------------------------------------------------------
# set_limit grow / shrink
# ---------------------------------------------------------------------------


class TestResizableLimiterSetLimit:
    def test_set_limit_clamps(self):
        lim = ResizableLimiter(initial=8)
        lim.set_limit(999)
        assert lim.limit == K_MAX
        lim.set_limit(0)
        assert lim.limit == K_MIN
        lim.set_limit(16)
        assert lim.limit == 16

    def test_set_limit_grow_wakes_parked_acquirers(self):
        lim = ResizableLimiter(initial=K_MIN)
        for _ in range(K_MIN):
            assert lim.acquire(timeout=1.0) is True

        woke = threading.Event()

        def waiter():
            if lim.acquire(timeout=5.0):
                woke.set()

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        time.sleep(0.1)
        assert not woke.is_set()

        # Grow the limit — a parked acquirer must proceed without any release
        lim.set_limit(K_MIN + 1)
        t.join(timeout=5.0)
        assert woke.is_set(), "set_limit grow must wake parked acquirers (notify_all)"

    def test_set_limit_shrink_does_not_kill_in_flight(self):
        lim = ResizableLimiter(initial=20)
        # 10 in-flight
        for _ in range(10):
            assert lim.acquire(timeout=1.0) is True
        assert lim.in_flight == 10

        # Shrink below the current in-flight count
        lim.set_limit(K_MIN)  # 8 < 10
        # In-flight calls are NOT killed — they keep running
        assert lim.in_flight == 10
        assert lim.limit == K_MIN

        # A new acquire must block (in_flight 10 >= limit 8)
        assert lim.acquire(timeout=0.1) is False

        # Releasing in-flight slots eventually lets a new acquire through
        for _ in range(3):
            lim.release()
        assert lim.in_flight == 7
        assert lim.acquire(timeout=1.0) is True


# ---------------------------------------------------------------------------
# Thread-safety under saturation
# ---------------------------------------------------------------------------


class TestResizableLimiterThreadSafety:
    def test_in_flight_never_exceeds_limit_under_load(self):
        limit = 8
        lim = ResizableLimiter(initial=limit)
        violations: List[int] = []
        observed_peak = [0]
        peak_lock = threading.Lock()
        start = threading.Event()

        def worker():
            start.wait(timeout=5.0)
            for _ in range(20):
                if lim.acquire(timeout=5.0):
                    cur = lim.in_flight
                    with peak_lock:
                        if cur > observed_peak[0]:
                            observed_peak[0] = cur
                        if cur > limit:
                            violations.append(cur)
                    # tiny hold to force contention
                    time.sleep(0.001)
                    lim.release()

        threads = [
            threading.Thread(target=worker, daemon=True) for _ in range(limit * 3)
        ]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(timeout=15.0)

        assert not violations, f"in_flight exceeded limit: peaks {violations}"
        assert lim.in_flight == 0, "all slots must be released at end"
        assert observed_peak[0] <= limit
        # Under heavy contention the peak should reach the limit
        assert lim.high_water == limit, (
            f"high_water {lim.high_water} should reach saturation limit {limit}"
        )
