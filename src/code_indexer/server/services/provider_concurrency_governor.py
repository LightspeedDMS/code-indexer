"""ProviderConcurrencyGovernor — per-LANE adaptive concurrency limiter.

Bug #1078 Phase 1 introduced this governor with 2 per-PROVIDER budgets
("voyage", "cohere"), each a fixed ``threading.BoundedSemaphore(K)``. Story
#1079 Phase B+C refines it into **4 independent per-LANE budgets**:

    voyage:embed   voyage:rerank   cohere:embed   cohere:rerank

Each lane owns its own:
  - ``ResizableLimiter`` (NOT a BoundedSemaphore) — a runtime-resizable
    concurrency limiter whose ``in_flight``/``high_water`` are the SINGLE SOURCE
    OF TRUTH for that lane's concurrency telemetry.
  - ``AimdController`` — additive-increase / multiplicative-decrease adaptive K.
    A success grows K (slowly, after a threshold); a 429 halves K. The
    controller drives the limiter via ``set_limit``.
  - one ``ProviderHealthMonitor`` sinbin health key.

Lanes are FULLY INDEPENDENT: a 429 on one lane's AIMD never changes another
lane's K, because each lane has its own limiter+condition+AIMD.

Design principles:
- Sinbin pre-check: if the lane's mapped health key is sinbinned, raise
  ProviderSinbinnedError FAST without consuming a slot.
- Initial K is seeded from server runtime config field
  ``query_provider_max_concurrency`` (default 8 when unset/unreadable), then
  CLAMPED into ``[K_MIN, K_MAX]`` = [8, 32].
- Telemetry: ``in_flight_high_water_mark`` reads each lane's ResizableLimiter
  ``high_water`` (single source of truth). ``acquire_wait_count`` is a
  governor-maintained contention counter (the limiter does not track waits).

Call-site pattern (sleep is OUTSIDE the slot — see provider_backoff.py):
    execute_with_backoff(lambda: governor.execute(lane, do_http, acquire_timeout=...))
"""

import logging
import threading
from typing import Callable, Dict, Optional, TypeVar

from code_indexer.server.services.aimd_controller import AimdController
from code_indexer.server.services.resizable_limiter import (
    K_MIN,
    ResizableLimiter,
)
from code_indexer.services.provider_backoff import is_rate_limited

logger = logging.getLogger(__name__)

# Default seed for the initial per-lane K when server runtime config is
# unavailable/unreadable. Per Story #1079 this defaults to the AIMD floor
# (K_MIN = 8); the constructor additionally clamps any seed into [K_MIN, K_MAX].
_DEFAULT_MAX_CONCURRENCY: int = K_MIN

# Lane -> the single ProviderHealthMonitor health key to check before acquiring.
_LANE_HEALTH_KEY: Dict[str, str] = {
    "voyage:embed": "voyage-ai",
    "voyage:rerank": "voyage-reranker",
    "cohere:embed": "cohere",
    "cohere:rerank": "cohere-reranker",
}

T = TypeVar("T")


class GovernorBusyError(RuntimeError):
    """Raised when all slots for a lane are occupied and acquire_timeout is exceeded."""

    def __init__(self, budget: str, timeout: float) -> None:
        self.budget = budget
        self.timeout = timeout
        super().__init__(
            f"All {budget!r} concurrency slots occupied after {timeout:.3f}s wait"
        )


class ProviderSinbinnedError(RuntimeError):
    """Raised when a lane is sinbinned — fast-fail without consuming a slot."""

    def __init__(self, budget: str, health_key: str) -> None:
        self.budget = budget
        self.health_key = health_key
        super().__init__(
            f"Provider lane {budget!r} sinbinned (health key: {health_key!r})"
        )


class ProviderConcurrencyGovernor:
    """Thread-safe 4-lane adaptive concurrency governor.

    Singleton (``get_instance()`` / ``reset_instance()``), mirroring the
    ProviderHealthMonitor singleton pattern. Construct directly (bypassing the
    singleton) in tests with an explicit ``max_concurrency`` seed.
    """

    _instance: Optional["ProviderConcurrencyGovernor"] = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self, max_concurrency: Optional[int] = None) -> None:
        if max_concurrency is None:
            max_concurrency = self._read_config_concurrency()
        # One ResizableLimiter + AimdController per lane. ResizableLimiter clamps
        # the seed into [K_MIN, K_MAX] internally; AimdController seeds its K from
        # the limiter's (clamped) limit, so the two always agree at construction.
        self._limiters: Dict[str, ResizableLimiter] = {
            lane: ResizableLimiter(initial=max_concurrency) for lane in _LANE_HEALTH_KEY
        }
        self._aimd: Dict[str, AimdController] = {
            lane: AimdController(limiter=self._limiters[lane])
            for lane in _LANE_HEALTH_KEY
        }
        # acquire_wait_count stays governor-maintained (the limiter does not
        # track contention). in_flight/high_water come from the limiters.
        self._stats_lock = threading.Lock()
        self._wait_count: Dict[str, int] = {lane: 0 for lane in _LANE_HEALTH_KEY}

    # ------------------------------------------------------------------
    # Singleton interface
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "ProviderConcurrencyGovernor":
        """Return the process-level singleton, creating it on first call."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Destroy the singleton (for test isolation)."""
        with cls._lock:
            cls._instance = None

    # ------------------------------------------------------------------
    # Introspection (used by tests + observability)
    # ------------------------------------------------------------------

    def aimd(self, budget: str) -> AimdController:
        """Return the AimdController for a lane (raises KeyError for unknown lane)."""
        return self._aimd[budget]

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def execute(
        self,
        budget: str,
        fn: Callable[[], T],
        *,
        acquire_timeout: float,
    ) -> T:
        """Gate a single HTTP attempt through the lane's ResizableLimiter.

        Steps:
          1. Validate lane key (raises KeyError for unknown lanes).
          2. Sinbin pre-check: if the lane's health key is sinbinned, raise
             ProviderSinbinnedError immediately WITHOUT consuming a slot.
          3. Probe in-flight; if already at K, record a waited acquire.
          4. Acquire the limiter within acquire_timeout; on timeout raise
             GovernorBusyError (bounded failure, never a hang).
          5. Run fn() (one HTTP attempt). On success: aimd.record(success=True).
             On a rate-limited exc (is_rate_limited): aimd.record(success=False)
             then re-raise. Release the slot in finally.

        Args:
            budget: One of the 4 lanes: "voyage:embed", "voyage:rerank",
                "cohere:embed", "cohere:rerank".
            fn: Zero-argument callable performing ONE HTTP call.
            acquire_timeout: Seconds to wait for a slot before GovernorBusyError.

        Returns:
            Whatever fn() returns.

        Raises:
            KeyError: Unknown lane.
            ProviderSinbinnedError: Lane is sinbinned (no slot consumed).
            GovernorBusyError: acquire_timeout elapsed before a slot was free.
            Any exception raised by fn() (slot released in finally; a 429 also
            triggers an AIMD multiplicative decrease before re-raising).
        """
        if budget not in _LANE_HEALTH_KEY:
            raise KeyError(f"Unknown governor lane {budget!r}")

        # Step 2: sinbin pre-check — fast-fail without a slot.
        self._check_sinbin(budget)

        limiter = self._limiters[budget]

        # Step 3: detect contention before acquiring.
        if limiter.in_flight >= limiter.limit:
            self._record_waited(budget)

        # Step 4: acquire the lane limiter.
        if not limiter.acquire(timeout=acquire_timeout):
            raise GovernorBusyError(budget, acquire_timeout)

        aimd = self._aimd[budget]
        try:
            result = fn()
            aimd.record(success=True)
            return result
        except BaseException as exc:
            # Per 429 ATTEMPT: multiplicative decrease via the lane's AIMD. Only
            # canonically-classified rate-limit signals count (Phase A).
            if is_rate_limited(exc):
                aimd.record(success=False)
            raise
        finally:
            limiter.release()

    # ------------------------------------------------------------------
    # Telemetry properties
    # ------------------------------------------------------------------

    @property
    def in_flight_high_water_mark(self) -> Dict[str, int]:
        """Per-lane peak concurrent in-flight count.

        SINGLE SOURCE OF TRUTH: read directly from each lane's ResizableLimiter
        ``high_water`` rather than a parallel hand-incremented governor counter.
        """
        return {lane: lim.high_water for lane, lim in self._limiters.items()}

    @property
    def acquire_wait_count(self) -> Dict[str, int]:
        """Per-lane count of acquisitions that had to wait (contention count)."""
        with self._stats_lock:
            return dict(self._wait_count)

    @property
    def current_k(self) -> Dict[str, int]:
        """Per-lane current adaptive concurrency K (observability).

        Reads each lane's AimdController ``k`` (lock-protected). The coalescer
        coalescing ratio and these K values are the primary Phase E observability
        surface for the per-lane adaptive limiter.
        """
        return {lane: aimd.k for lane, aimd in self._aimd.items()}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_sinbin(self, budget: str) -> None:
        """Raise ProviderSinbinnedError if the lane's health key is sinbinned."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        monitor = ProviderHealthMonitor.get_instance()
        key = _LANE_HEALTH_KEY[budget]
        if monitor.is_sinbinned(key):
            raise ProviderSinbinnedError(budget, key)

    def _record_waited(self, budget: str) -> None:
        """Increment wait_count for a lane (contention detected before acquire)."""
        with self._stats_lock:
            self._wait_count[budget] += 1

    @staticmethod
    def _read_config_concurrency() -> int:
        """Read query_provider_max_concurrency from server runtime config.

        Falls back to _DEFAULT_MAX_CONCURRENCY (= K_MIN = 8) on any error (config
        not yet initialized, missing field, test context, etc.). The returned
        value is clamped into [K_MIN, K_MAX] by the constructor.
        """
        try:
            from code_indexer.server.services.config_service import get_config_service

            cfg = get_config_service().get_config()
            value = getattr(cfg, "query_provider_max_concurrency", None)
            if isinstance(value, int) and value > 0:
                return value
        except Exception as exc:
            logger.debug(
                "ProviderConcurrencyGovernor: could not read config (%s); "
                "using default K=%d",
                exc,
                _DEFAULT_MAX_CONCURRENCY,
            )
        return _DEFAULT_MAX_CONCURRENCY
