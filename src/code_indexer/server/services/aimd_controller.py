"""AimdController — per-lane adaptive concurrency (Story #1079 Phase B).

Additive-Increase / Multiplicative-Decrease (AIMD) controller for one provider
lane. Drives a ``ResizableLimiter`` via ``set_limit(K)``:

- On a 429 (rate-limit, classified canonically by the governor via
  ``provider_backoff.is_rate_limited``): MULTIPLICATIVE DECREASE — K = max(K_MIN,
  K // 2). A cooldown window starts so a flurry of in-flight successes that land
  right after the cut does not immediately re-grow K.
- On a success OUTSIDE cooldown: count a success run; once SUCCESS_THRESHOLD
  consecutive successes accumulate, ADDITIVE INCREASE — K = min(K_MAX, K + 1).

For ``:embed`` lanes K is the number of concurrent BATCHES; for ``:rerank``
lanes K is the number of concurrent rerank calls.

Thread-safety: ``record()`` mutates AIMD state and calls ``limiter.set_limit()``
under the limiter's OWN condition lock (shared lock domain). Because the
limiter's Condition uses a re-entrant RLock, calling ``set_limit`` (which
re-acquires the same lock) from inside ``record`` is safe and keeps the AIMD
state mutation + limit change atomic with respect to acquire/release. This is
why lanes are fully independent: each lane has its own limiter+condition+AIMD,
so a 429 on one lane can never perturb another lane's K.

Determinism: ``time_fn`` defaults to ``time.monotonic`` but is injectable so the
cooldown logic is unit-testable without sleeping. NEVER use ``time.sleep`` here.
"""

import logging
import threading
import time
from typing import Callable

from code_indexer.server.services.resizable_limiter import (
    K_MAX,
    K_MIN,
    ResizableLimiter,
)

logger = logging.getLogger(__name__)

# Consecutive successes (outside cooldown) required before one additive +1 step.
# Small enough to recover throughput within a few hundred ms of healthy calls,
# large enough not to oscillate. Phase E may expose this as a config seed.
SUCCESS_THRESHOLD: int = 8

# Seconds after a multiplicative decrease during which successes do NOT re-grow
# K. Gives the provider's token bucket time to refill before probing upward
# again. Phase E may expose this as a config seed.
COOLDOWN_SECONDS: float = 3.0


class AimdController:
    """Per-lane AIMD adaptive-K controller bound to a ResizableLimiter."""

    def __init__(
        self,
        limiter: ResizableLimiter,
        *,
        time_fn: Callable[[], float] = time.monotonic,
        k_min: int = K_MIN,
        k_max: int = K_MAX,
    ) -> None:
        self._limiter = limiter
        self._time_fn = time_fn
        # Per-instance AIMD floor/ceiling. Default to the module constants
        # [K_MIN, K_MAX] = [8, 32] so existing direct-construction callers/tests
        # are unaffected; the governor seeds these from config.coalesce_k_min /
        # config.coalesce_k_max (Story #1079 anti-orphan fix). The bound clamps
        # are read at the decrease floor / increase ceiling below.
        self._k_min: int = k_min
        self._k_max: int = k_max
        # AIMD state — all reads/writes happen under the limiter's condition lock.
        # Seed K from the limiter's current (already-clamped) limit so the AIMD
        # target and the limiter's enforced limit agree at construction. The
        # limiter clamps into [K_MIN, K_MAX], so this is always in range.
        self._k: int = limiter.limit
        self._success_run: int = 0
        self._cooldown_until: float = 0.0
        # Alias the limiter's condition so record() shares its lock domain.
        self._lock: threading.Condition = limiter.condition

    @property
    def k(self) -> int:
        """Current target concurrency K (lock-protected read)."""
        with self._lock:
            return self._k

    def record(self, *, success: bool) -> None:
        """Record one call outcome and adjust K accordingly.

        Args:
            success: True for a successful HTTP attempt; False for a 429
                (rate-limited) attempt. The governor decides which by calling
                ``is_rate_limited(exc)`` — this controller only sees the boolean,
                and is invoked once per 429 ATTEMPT (not per request).
        """
        with self._lock:
            if not success:
                # Multiplicative decrease — never below the floor.
                old_k = self._k
                new_k = max(self._k_min, self._k // 2)
                # Observability (Phase E): structured WARNING ONLY when K actually
                # drops. At the floor (new_k == old_k) there is nothing to report.
                if new_k != old_k:
                    logger.warning(
                        "AIMD multiplicative decrease: K %d -> %d (429)",
                        old_k,
                        new_k,
                        extra={"old_k": old_k, "new_k": new_k},
                    )
                self._k = new_k
                self._success_run = 0
                self._cooldown_until = self._time_fn() + COOLDOWN_SECONDS
                self._limiter.set_limit(self._k)
                return

            # Success path — suppressed during the post-decrease cooldown so a
            # burst of in-flight successes that complete right after a cut does
            # not immediately re-grow K.
            if self._time_fn() < self._cooldown_until:
                return

            self._success_run += 1
            if self._success_run >= SUCCESS_THRESHOLD and self._k < self._k_max:
                self._k += 1
                self._success_run = 0
                self._limiter.set_limit(self._k)
