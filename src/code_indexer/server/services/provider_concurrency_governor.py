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
  ``query_provider_max_concurrency`` (PER-NODE total budget; default 8 when
  unset/unreadable). Story #1165: at construction the per-node budget is
  divided by ``config.workers`` so combined embedding pressure across all
  uvicorn workers stays within the per-node limit. Per-worker K =
  max(k_min, per_node_budget // workers), then CLAMPED into ``[k_min, k_max]``.
  Workers=1 is byte-identical to the pre-#1165 behavior.
- The K bounds ``[k_min, k_max]`` (the AIMD floor/ceiling AND the per-lane
  limiter clamp) are themselves seeded from config ``coalesce_k_min`` /
  ``coalesce_k_max`` (defaults ``[K_MIN, K_MAX] = [8, 32]``; valid range
  ``8 <= k_min <= k_max <= 256``). This is a CONSTRUCTION-SCOPED seed, NOT a
  live hot-reload knob — like ``query_provider_max_concurrency``, the bounds are
  baked in at construction and only change on the next governor construction /
  server restart. Invalid config falls back to the 8/32 defaults with a logged
  WARNING (see ``_read_config_k_bounds``).
- Telemetry: ``in_flight_high_water_mark`` reads each lane's ResizableLimiter
  ``high_water`` (single source of truth). ``acquire_wait_count`` is a
  governor-maintained contention counter (the limiter does not track waits).

Call-site pattern (sleep is OUTSIDE the slot — see provider_backoff.py):
    execute_with_backoff(lambda: governor.execute(lane, do_http, acquire_timeout=...))
"""

import logging
import threading
from typing import Callable, Dict, Optional, Tuple, TypeVar

from code_indexer.server.services.aimd_controller import AimdController
from code_indexer.server.services.resizable_limiter import (
    K_MAX,
    K_MIN,
    ResizableLimiter,
)
from code_indexer.services.provider_backoff import is_rate_limited

logger = logging.getLogger(__name__)

# Default seed for the initial per-lane K when server runtime config is
# unavailable/unreadable. Per Story #1079 this defaults to the AIMD floor
# (K_MIN = 8); the constructor additionally clamps any seed into [k_min, k_max].
_DEFAULT_MAX_CONCURRENCY: int = K_MIN

# Absolute ceiling for the configurable AIMD K_MAX (coalesce_k_max). A sane upper
# bound so an operator typo (e.g. 100000) cannot create a runaway concurrency
# ceiling. Config values above this are rejected and fall back to the defaults.
_K_MAX_HARD_CEILING: int = 256

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
        # K bounds (the AIMD floor/ceiling + limiter clamp) are a CONSTRUCTION-
        # SCOPED seed read from config.coalesce_k_min / config.coalesce_k_max,
        # NOT a live hot-reload knob (mirrors how query_provider_max_concurrency
        # seeds the initial K at construction). A change to these config fields
        # takes effect on the next governor construction / server restart.
        #
        # Explicit max_concurrency (direct construction in tests) keeps the
        # default [K_MIN, K_MAX] = [8, 32] bounds so existing tests are
        # unaffected; only the auto-seed path consults config for the bounds.
        if max_concurrency is None:
            k_min, k_max = self._read_config_k_bounds()
            per_node_seed = self._read_config_concurrency()
            worker_count = self._read_config_workers()
            # Story #1165: query_provider_max_concurrency is the PER-NODE total
            # provider-concurrency budget. Divide it across this node's uvicorn
            # workers so combined embedding pressure across all workers stays within
            # the configured per-node budget. Floor via the [k_min, k_max] clamp below.
            max_concurrency = max(k_min, per_node_seed // worker_count)
        else:
            k_min, k_max = K_MIN, K_MAX
        # Clamp the initial K seed into the (possibly config-widened) bounds.
        seed = min(max(max_concurrency, k_min), k_max)
        # One ResizableLimiter + AimdController per lane. The limiter clamps into
        # [k_min, k_max]; AimdController seeds its K from the limiter's (clamped)
        # limit and uses the same [k_min, k_max] floor/ceiling, so the two always
        # agree at construction.
        self._limiters: Dict[str, ResizableLimiter] = {
            lane: ResizableLimiter(initial=seed, k_min=k_min, k_max=k_max)
            for lane in _LANE_HEALTH_KEY
        }
        self._aimd: Dict[str, AimdController] = {
            lane: AimdController(limiter=self._limiters[lane], k_min=k_min, k_max=k_max)
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

        This value is the PER-DEPLOYMENT-NODE total provider-concurrency budget.
        Story #1165: the constructor (auto-seed path only) divides this per-node
        budget by ``config.workers`` so that the combined embedding pressure across
        all uvicorn workers on the node stays within the configured limit.
        Cross-node budgeting remains the operator's responsibility.

        Falls back to _DEFAULT_MAX_CONCURRENCY (= K_MIN = 8) on any error (config
        not yet initialized, missing field, test context, etc.). The returned
        value is divided by worker_count then clamped into [k_min, k_max] by the
        constructor.
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

    @staticmethod
    def _read_config_workers() -> int:
        """Read config.workers (number of uvicorn workers on this node).

        Story #1165: query_provider_max_concurrency is the PER-NODE total
        provider-concurrency budget. This helper reads the worker count so the
        constructor can divide the per-node budget across workers.

        Falls back to 1 (no division) on any error (config not yet initialized,
        missing field, non-int, zero, negative, test context, etc.). The
        ``max(1, value)`` guard prevents division-by-zero and ensures a
        misconfigured 0 or negative count is treated as a single worker.

        Per-node scope: this divides ONE node's budget across that node's
        workers. Cross-node budgeting remains the operator's responsibility
        (one ``query_provider_max_concurrency`` per node).

        Story #1197 AC5 (CRITICAL-C2): reads the APPLIED worker count via the
        applied_worker_count resolver (applied_launch.json → config.json → 1),
        never get_config().workers (the TARGET, which may have been saved but
        not yet restarted into effect).  This prevents a node still running the
        old worker count from dividing its budget by the new unapplied TARGET.
        """
        try:
            from code_indexer.server.services.applied_worker_count import (
                get_applied_worker_count,
            )

            return int(get_applied_worker_count())
        except Exception as exc:
            logger.debug(
                "ProviderConcurrencyGovernor: could not read applied worker count (%s); "
                "using worker_count=1 (no per-worker division)",
                exc,
            )
        return 1

    @staticmethod
    def _read_config_k_bounds() -> Tuple[int, int]:
        """Read coalesce_k_min/coalesce_k_max (the AIMD floor/ceiling seeds).

        CONSTRUCTION-SCOPED seed, NOT a live hot-reload knob — the returned
        bounds are baked into the per-lane limiter clamp + AIMD floor/ceiling at
        construction and only change on the next governor construction / restart.

        Validation: requires ``K_MIN <= k_min <= k_max <= _K_MAX_HARD_CEILING``.
        On ANY failure (config not yet initialized, missing field, non-int,
        out-of-range, k_min > k_max, test context) falls back to the documented
        module defaults ``(K_MIN, K_MAX) = (8, 32)``. Present-but-invalid values
        emit a structured WARNING; missing fields and unreadable config log at
        DEBUG. Mirrors the ``_read_config_concurrency`` fallback discipline.
        """
        try:
            from code_indexer.server.services.config_service import get_config_service

            cfg = get_config_service().get_config()
            k_min = getattr(cfg, "coalesce_k_min", None)
            k_max = getattr(cfg, "coalesce_k_max", None)
            if k_min is None or k_max is None:
                # Missing field(s): use defaults (matches an un-migrated config
                # that predates the coalesce_k_* fields). DEBUG, not WARNING —
                # absence is expected/benign, unlike present-but-invalid values.
                logger.debug(
                    "ProviderConcurrencyGovernor: coalesce K bounds absent "
                    "(k_min=%r, k_max=%r); using defaults (%d, %d)",
                    k_min,
                    k_max,
                    K_MIN,
                    K_MAX,
                )
                return K_MIN, K_MAX
            if (
                isinstance(k_min, int)
                and isinstance(k_max, int)
                and K_MIN <= k_min <= k_max <= _K_MAX_HARD_CEILING
            ):
                return k_min, k_max
            # Present but invalid -> WARNING + documented defaults.
            logger.warning(
                "ProviderConcurrencyGovernor: invalid coalesce K bounds "
                "(k_min=%r, k_max=%r); require %d <= k_min <= k_max <= %d. "
                "Falling back to defaults (%d, %d).",
                k_min,
                k_max,
                K_MIN,
                _K_MAX_HARD_CEILING,
                K_MIN,
                K_MAX,
            )
        except Exception as exc:
            logger.debug(
                "ProviderConcurrencyGovernor: could not read coalesce K bounds "
                "(%s); using defaults (%d, %d)",
                exc,
                K_MIN,
                K_MAX,
            )
        return K_MIN, K_MAX
