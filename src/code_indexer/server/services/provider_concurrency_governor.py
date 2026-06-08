"""ProviderConcurrencyGovernor — per-budget concurrency limiter (Bug #1078 Phase 1).

Prevents semantic-search concurrency collapse by gating all serving-path
embedding and rerank HTTP calls through a per-budget BoundedSemaphore.

Design principles:
- One semaphore per account-level rate budget: "voyage" and "cohere".
  Voyage embedding AND Voyage rerank share the "voyage" semaphore because
  they share the same VoyageAI account rate limit.
- Sinbin pre-check: if ANY mapped ProviderHealthMonitor health key is
  sinbinned, raise ProviderSinbinnedError FAST without consuming a slot.
- K (max concurrency per budget) comes from server runtime config field
  ``query_provider_max_concurrency`` (default 16), read once at construction.
- Expose ``in_flight_high_water_mark`` and ``acquire_wait_count`` per budget
  for test assertions and observability.

Call-site pattern (sleep is OUTSIDE the slot — see provider_backoff.py):
    execute_with_backoff(lambda: governor.execute(budget, do_http, acquire_timeout=...))
"""

import logging
import threading
from typing import Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

# Default max-concurrency per budget (overridden via server runtime config).
_DEFAULT_MAX_CONCURRENCY: int = 16

# Budget -> list of ProviderHealthMonitor health keys to check before acquiring.
_BUDGET_HEALTH_KEYS: Dict[str, List[str]] = {
    "voyage": ["voyage-ai", "voyage-reranker"],
    "cohere": ["cohere", "cohere-reranker"],
}

T = TypeVar("T")


class GovernorBusyError(RuntimeError):
    """Raised when all slots for a budget are occupied and acquire_timeout is exceeded."""

    def __init__(self, budget: str, timeout: float) -> None:
        self.budget = budget
        self.timeout = timeout
        super().__init__(
            f"All {budget!r} concurrency slots occupied after {timeout:.3f}s wait"
        )


class ProviderSinbinnedError(RuntimeError):
    """Raised when a provider budget is sinbinned — fast-fail without consuming a slot."""

    def __init__(self, budget: str, health_key: str) -> None:
        self.budget = budget
        self.health_key = health_key
        super().__init__(
            f"Provider budget {budget!r} sinbinned (health key: {health_key!r})"
        )


class ProviderConcurrencyGovernor:
    """Thread-safe per-budget BoundedSemaphore governor.

    Singleton (``get_instance()`` / ``reset_instance()``), mirroring the
    ProviderHealthMonitor singleton pattern.

    Construct directly (bypassing singleton) in tests by calling the
    constructor with an explicit ``max_concurrency``.
    """

    _instance: Optional["ProviderConcurrencyGovernor"] = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self, max_concurrency: Optional[int] = None) -> None:
        if max_concurrency is None:
            max_concurrency = self._read_config_concurrency()
        self._k: int = max_concurrency
        # One BoundedSemaphore per budget
        self._semaphores: Dict[str, threading.BoundedSemaphore] = {
            budget: threading.BoundedSemaphore(self._k)
            for budget in _BUDGET_HEALTH_KEYS
        }
        # Telemetry counters — all reads/writes under self._stats_lock
        self._stats_lock = threading.Lock()
        self._in_flight: Dict[str, int] = {b: 0 for b in _BUDGET_HEALTH_KEYS}
        self._high_water: Dict[str, int] = {b: 0 for b in _BUDGET_HEALTH_KEYS}
        self._wait_count: Dict[str, int] = {b: 0 for b in _BUDGET_HEALTH_KEYS}

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
    # Core API
    # ------------------------------------------------------------------

    def execute(
        self,
        budget: str,
        fn: Callable[[], T],
        *,
        acquire_timeout: float,
    ) -> T:
        """Gate a single HTTP attempt through the budget semaphore.

        Steps:
          1. Validate budget key (raises KeyError for unknown budgets).
          2. Sinbin pre-check: if ANY mapped health key is sinbinned, raise
             ProviderSinbinnedError immediately WITHOUT consuming a slot.
          3. Probe current in-flight count; if already at K, this acquire will
             block — record it as a waited acquire.
          4. Try to acquire the semaphore within acquire_timeout seconds.
             If the timeout elapses, raise GovernorBusyError.
          5. Run fn() (one HTTP attempt). Release slot in finally.

        Args:
            budget: One of "voyage" or "cohere".
            fn: Zero-argument callable performing ONE HTTP call. Its return
                value is propagated to the caller.
            acquire_timeout: Seconds to wait for a slot before raising
                GovernorBusyError.

        Returns:
            Whatever fn() returns.

        Raises:
            KeyError: Unknown budget.
            ProviderSinbinnedError: Budget is sinbinned (no slot consumed).
            GovernorBusyError: acquire_timeout elapsed before a slot was free.
            Any exception raised by fn() (slot is released in finally).
        """
        if budget not in _BUDGET_HEALTH_KEYS:
            raise KeyError(f"Unknown governor budget {budget!r}")

        # Step 2: sinbin pre-check — fast-fail without a slot
        self._check_sinbin(budget)

        # Step 3: detect contention before acquiring.
        # If in_flight is already at K, this acquire will have to wait.
        with self._stats_lock:
            will_wait = self._in_flight[budget] >= self._k

        if will_wait:
            self._record_waited(budget)

        # Step 4: acquire semaphore
        sem = self._semaphores[budget]
        acquired = sem.acquire(blocking=True, timeout=acquire_timeout)
        if not acquired:
            raise GovernorBusyError(budget, acquire_timeout)

        # Update in-flight and high-water under stats lock
        with self._stats_lock:
            self._in_flight[budget] += 1
            if self._in_flight[budget] > self._high_water[budget]:
                self._high_water[budget] = self._in_flight[budget]

        try:
            return fn()
        finally:
            with self._stats_lock:
                self._in_flight[budget] -= 1
            sem.release()

    # ------------------------------------------------------------------
    # Telemetry properties
    # ------------------------------------------------------------------

    @property
    def in_flight_high_water_mark(self) -> Dict[str, int]:
        """Per-budget peak concurrent in-flight count (for test assertions)."""
        with self._stats_lock:
            return dict(self._high_water)

    @property
    def acquire_wait_count(self) -> Dict[str, int]:
        """Per-budget count of acquisitions that had to wait (contention count)."""
        with self._stats_lock:
            return dict(self._wait_count)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_sinbin(self, budget: str) -> None:
        """Raise ProviderSinbinnedError if any mapped health key is sinbinned."""
        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        monitor = ProviderHealthMonitor.get_instance()
        for key in _BUDGET_HEALTH_KEYS[budget]:
            if monitor.is_sinbinned(key):
                raise ProviderSinbinnedError(budget, key)

    def _record_waited(self, budget: str) -> None:
        """Increment wait_count for a budget (contention detected before acquire)."""
        with self._stats_lock:
            self._wait_count[budget] += 1

    @staticmethod
    def _read_config_concurrency() -> int:
        """Read query_provider_max_concurrency from server runtime config.

        Falls back to _DEFAULT_MAX_CONCURRENCY on any error (config not yet
        initialized, missing field, test context, etc.).
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
