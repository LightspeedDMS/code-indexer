"""ResizableLimiter — runtime-resizable concurrency limiter (Story #1079 Phase B).

Replaces ``threading.BoundedSemaphore`` for the per-lane concurrency governor.
A BoundedSemaphore cannot grow or shrink its bound at runtime and refuses to
release above its initial count, which makes it unusable for AIMD adaptive
concurrency (where K changes over time). ResizableLimiter solves this with a
single ``threading.Condition`` guarding all state.

Single source of truth: ``in_flight`` (and its peak ``high_water``) are the
authoritative per-lane concurrency telemetry. The governor reads these directly
rather than maintaining a parallel hand-incremented counter.

Design (matches Story #1079 authoritative pseudocode):
- acquire(timeout): waits (bounded by timeout) until in_flight < limit, then
  increments in_flight and updates high_water. Returns False on timeout — NEVER
  hangs, NEVER raises.
- release(): decrements in_flight and notifies one parked acquirer.
- set_limit(new): clamps to this limiter's [k_min, k_max] (default [8, 32]; the
  governor seeds per-instance bounds from config.coalesce_k_min/coalesce_k_max)
  and notify_all() so parked acquirers re-evaluate the predicate on growth.
  Shrinking NEVER kills in-flight work — it only makes future acquires wait until
  releases bring in_flight below the new limit.

All loops have a provable termination bound: acquire's wait loop exits when the
monotonic deadline passes (Messi #14).
"""

import threading
import time

# AIMD concurrency bounds — shared with AimdController (Story #1079 Phase B).
# These remain the DEFAULT clamp bounds. The governor seeds per-instance bounds
# from config.coalesce_k_min / config.coalesce_k_max (Story #1079 anti-orphan
# fix); direct-construction callers that pass no bounds keep [8, 32].
K_MIN: int = 8
K_MAX: int = 32


def _clamp_limit(value: int, k_min: int = K_MIN, k_max: int = K_MAX) -> int:
    """Clamp a proposed limit into the closed range [k_min, k_max].

    Defaults to the module constants [K_MIN, K_MAX] = [8, 32] so existing
    callers are unaffected; the governor passes config-seeded bounds.
    """
    if value < k_min:
        return k_min
    if value > k_max:
        return k_max
    return value


class ResizableLimiter:
    """Lock+condition concurrency limiter with a runtime-resizable bound.

    Thread-safe. All state (``limit``, ``in_flight``, ``high_water``) is guarded
    by a single ``threading.Condition``; AimdController shares this condition's
    lock domain so AIMD state mutation + set_limit are atomic with respect to the
    limiter (see aimd_controller.py).
    """

    def __init__(self, initial: int, *, k_min: int = K_MIN, k_max: int = K_MAX) -> None:
        self._cond = threading.Condition()
        # Per-instance clamp bounds. Default to the module constants [8, 32] so
        # existing direct-construction callers/tests are unaffected; the governor
        # seeds these from config.coalesce_k_min / config.coalesce_k_max.
        self._k_min: int = k_min
        self._k_max: int = k_max
        self._limit: int = _clamp_limit(initial, k_min, k_max)
        self._in_flight: int = 0
        self._high_water: int = 0

    # ------------------------------------------------------------------
    # Lock domain accessor (shared with AimdController)
    # ------------------------------------------------------------------

    @property
    def condition(self) -> threading.Condition:
        """The Condition guarding this limiter's state.

        AimdController acquires this same condition so its record()/set_limit
        sequence executes atomically within the limiter's lock domain.
        """
        return self._cond

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    def acquire(self, timeout: float) -> bool:
        """Acquire one slot, waiting up to ``timeout`` seconds.

        Returns True if a slot was acquired, False if the timeout elapsed first.
        Never raises, never hangs (the wait loop is bounded by a monotonic
        deadline).
        """
        with self._cond:
            deadline = time.monotonic() + timeout
            while self._in_flight >= self._limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
            self._in_flight += 1
            if self._in_flight > self._high_water:
                self._high_water = self._in_flight
            return True

    def release(self) -> None:
        """Release one slot and wake one parked acquirer."""
        with self._cond:
            self._in_flight -= 1
            self._cond.notify()

    # ------------------------------------------------------------------
    # Runtime resize
    # ------------------------------------------------------------------

    def set_limit(self, new_limit: int) -> None:
        """Set the concurrency limit, clamped to this limiter's [k_min, k_max].

        On growth, parked acquirers may now proceed, so notify_all() wakes them
        to re-check the predicate. On shrink, in-flight work is untouched; only
        future acquires wait until releases bring in_flight below the new limit.
        """
        with self._cond:
            self._limit = _clamp_limit(new_limit, self._k_min, self._k_max)
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Telemetry (single source of truth)
    # ------------------------------------------------------------------

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @property
    def high_water(self) -> int:
        with self._cond:
            return self._high_water
