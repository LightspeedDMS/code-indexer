"""Provider health monitoring service (Story #491).

Tracks per-provider embedding API metrics in a rolling window.
Thread-safe singleton for use across query and indexing operations.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

import numpy as np

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import ProviderSinBinConfig

logger = logging.getLogger(__name__)


@dataclass
class HealthMetric:
    """Single API call metric."""

    timestamp: float
    latency_ms: float
    success: bool
    provider: str


@dataclass
class ProviderHealthStatus:
    """Computed health status for a provider."""

    provider: str
    status: str  # "healthy", "degraded", "down", "sinbinned"
    health_score: float  # 0.0 to 1.0
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    error_rate: float  # 0.0 to 1.0
    availability: float  # 0.0 to 1.0
    total_requests: int
    successful_requests: int
    failed_requests: int
    window_minutes: int
    sinbinned: bool = field(default=False)  # Bug #678: circuit-breaker state


# Default thresholds
DEFAULT_ERROR_RATE_THRESHOLD = 0.1  # 10%
DEFAULT_LATENCY_P95_THRESHOLD_MS = 5000.0  # 5 seconds
DEFAULT_AVAILABILITY_THRESHOLD = 0.95  # 95%
DEFAULT_ROLLING_WINDOW_MINUTES = 60
DEFAULT_DOWN_ERROR_RATE = 0.5  # 50% error rate = down
DEFAULT_DOWN_CONSECUTIVE_FAILURES = 5

# Bug #678: Sentinel value for "no sin-bin active" monotonic timestamp
_SINBIN_NOT_ACTIVE: float = 0.0


class ProviderHealthMonitor:
    """Thread-safe provider health monitoring with rolling window."""

    _instance: Optional["ProviderHealthMonitor"] = None
    _lock = threading.Lock()

    def __init__(
        self,
        rolling_window_minutes: int = DEFAULT_ROLLING_WINDOW_MINUTES,
        error_rate_threshold: float = DEFAULT_ERROR_RATE_THRESHOLD,
        latency_p95_threshold_ms: float = DEFAULT_LATENCY_P95_THRESHOLD_MS,
        availability_threshold: float = DEFAULT_AVAILABILITY_THRESHOLD,
    ):
        self._metrics: Dict[str, deque] = {}  # provider -> deque of HealthMetric
        self._consecutive_failures: Dict[str, int] = {}
        self._rolling_window_minutes = rolling_window_minutes
        self._error_rate_threshold = error_rate_threshold
        self._latency_p95_threshold_ms = latency_p95_threshold_ms
        self._availability_threshold = availability_threshold
        self._data_lock = threading.Lock()
        # Story #619 Gap 4: recovery probe state
        self._probe_lock = threading.Lock()
        self._probe_threads: Dict[str, threading.Thread] = {}
        self._probe_stop_events: Dict[str, threading.Event] = {}
        self._last_known_status: Dict[str, str] = {}
        # Story #619 HIGH-2: registered probe functions per provider
        self._probe_functions: Dict[str, object] = {}
        # Bug #678: sin-bin (circuit-breaker) state
        self._sinbin_until: Dict[str, float] = {}  # provider -> monotonic expiry
        self._sinbin_rounds: Dict[
            str, int
        ] = {}  # provider -> consecutive sin-bin count
        self._sinbin_failure_deque: Dict[
            str, deque
        ] = {}  # provider -> recent failure timestamps

    @classmethod
    def get_instance(cls, **kwargs: object) -> "ProviderHealthMonitor":
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)  # type: ignore[arg-type]
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        with cls._lock:
            cls._instance = None

    def register_probe(self, provider_name: str, probe_fn: object) -> None:
        """Register a lightweight probe function for a provider (Story #619 HIGH-2).

        The probe_fn is called by _probe_loop during recovery to test real
        connectivity instead of recording a synthetic success.

        Args:
            provider_name: Provider identifier (e.g. "voyage-ai", "cohere").
            probe_fn: Callable with signature () -> bool. Must return True if
                      the provider is reachable, False otherwise. May raise; the
                      probe loop treats any exception as failure.
        """
        self._probe_functions[provider_name] = probe_fn

    # ------------------------------------------------------------------
    # Bug #678: Sin-bin (circuit-breaker) methods
    # ------------------------------------------------------------------

    def is_sinbinned(self, provider: str) -> bool:
        """Return True if provider is currently in sin-bin (cooldown active)."""
        with self._data_lock:
            return time.monotonic() < self._sinbin_until.get(
                provider, _SINBIN_NOT_ACTIVE
            )

    def sinbin(self, provider: str) -> None:
        """Place provider in sin-bin with exponential backoff cooldown."""
        cfg = self._get_sinbin_config(provider)
        with self._data_lock:
            rounds = self._sinbin_rounds.get(provider, 0)
            cooldown = min(
                cfg.initial_cooldown_seconds * (cfg.backoff_multiplier**rounds),
                cfg.max_cooldown_seconds,
            )
            self._sinbin_until[provider] = time.monotonic() + cooldown
            self._sinbin_rounds[provider] = rounds + 1
        logger.warning(
            "Provider '%s' sin-binned for %.1fs (round %d)",
            provider,
            cooldown,
            rounds + 1,
        )

    def clear_sinbin(self, provider: str) -> None:
        """Remove provider from sin-bin immediately and reset backoff rounds."""
        with self._data_lock:
            self._sinbin_until.pop(provider, None)
            self._sinbin_rounds[provider] = 0

    def get_sinbin_ttl_seconds(self, provider: str) -> Optional[float]:
        """Return remaining sin-bin cooldown in seconds, or None if not sinbinned."""
        with self._data_lock:
            expiry = self._sinbin_until.get(provider)
            if expiry is None:
                return None
            ttl = expiry - time.monotonic()
            return max(0.0, ttl) if ttl > 0 else None

    def get_sinbin_rounds(self, provider: str) -> int:
        """Return the number of consecutive sin-bin rounds for a provider."""
        with self._data_lock:
            return self._sinbin_rounds.get(provider, 0)

    def _get_sinbin_config(self, provider: str) -> "ProviderSinBinConfig":
        """Read sin-bin config from server runtime config, falling back to defaults."""
        from code_indexer.server.utils.config_manager import ProviderSinBinConfig

        try:
            from code_indexer.server.services.config_service import get_config_service

            server_cfg = get_config_service().get_config()
            if provider in ("voyage-ai", "voyage-reranker"):
                cfg = getattr(server_cfg, "voyage_ai_sinbin", None)
            elif provider in ("cohere", "cohere-reranker"):
                cfg = getattr(server_cfg, "cohere_sinbin", None)
            else:
                cfg = None
            if isinstance(cfg, ProviderSinBinConfig):
                return cfg
        except Exception as exc:
            logger.debug(
                "Sin-bin config read failed for provider '%s'; using defaults: %s",
                provider,
                exc,
            )
        return ProviderSinBinConfig()

    def reconfigure(self, provider: str) -> None:
        """Re-read thresholds from config for provider without losing accumulated metrics."""
        # Config is read on-demand in _get_sinbin_config; no state to update here.
        logger.debug("reconfigure called for provider '%s'", provider)

    # Recovery probe constants (Story #619 Gap 4)
    PROBE_INTERVAL_SEC: int = 30
    PROBE_JOIN_TIMEOUT_SEC: int = 5
    SYNTHETIC_PROBE_LATENCY_MS: float = 0.0

    def record_call(self, provider: str, latency_ms: float, success: bool) -> None:
        """Record an API call metric. Thread-safe."""
        metric = HealthMetric(
            timestamp=time.time(),
            latency_ms=latency_ms,
            success=success,
            provider=provider,
        )

        # Read sinbin config outside the lock to avoid holding it during I/O
        cfg = self._get_sinbin_config(provider)
        should_sinbin = False

        with self._data_lock:
            if provider not in self._metrics:
                self._metrics[provider] = deque()
            self._metrics[provider].append(metric)

            # Track consecutive failures
            if success:
                self._consecutive_failures[provider] = 0
                # Bug #678: on success after sinbin, reset rounds and clear entry
                if self._sinbin_rounds.get(provider, 0) > 0:
                    self._sinbin_rounds[provider] = 0
                    self._sinbin_until.pop(provider, None)
            else:
                self._consecutive_failures[provider] = (
                    self._consecutive_failures.get(provider, 0) + 1
                )
                # Bug #678: track failure in windowed deque
                if provider not in self._sinbin_failure_deque:
                    self._sinbin_failure_deque[provider] = deque()
                now_mono = time.monotonic()
                self._sinbin_failure_deque[provider].append(now_mono)
                # Prune entries outside window
                window_start = now_mono - cfg.failure_window_seconds
                fdeque = self._sinbin_failure_deque[provider]
                while fdeque and fdeque[0] < window_start:
                    fdeque.popleft()
                # Auto-sinbin when threshold reached
                if len(fdeque) >= cfg.failure_threshold:
                    should_sinbin = True

            # Prune old entries
            self._prune_old_metrics(provider)

            # Story #619 Gap 4: detect status transitions and manage recovery probe
            old_status = self._last_known_status.get(provider, "healthy")
            new_status = self._compute_status(provider).status
            self._last_known_status[provider] = new_status

        if should_sinbin:
            self.sinbin(provider)

        if old_status != "down" and new_status == "down":
            self._start_recovery_probe(provider)
        elif old_status == "down" and new_status != "down":
            self._stop_recovery_probe(provider)

    def _start_recovery_probe(self, provider_name: str) -> None:
        """Start background probe for a down provider (Story #619 Gap 4)."""
        with self._probe_lock:
            if provider_name in self._probe_threads:
                return  # already probing — idempotent
            stop_event = threading.Event()
            self._probe_stop_events[provider_name] = stop_event
            thread = threading.Thread(
                target=self._probe_loop,
                args=(provider_name, stop_event),
                daemon=True,
                name=f"recovery-probe-{provider_name}",
            )
            self._probe_threads[provider_name] = thread
        thread.start()
        logger.info("Started recovery probe for provider '%s'", provider_name)

    def _stop_recovery_probe(self, provider_name: str) -> None:
        """Stop background probe for a provider (Story #619 Gap 4)."""
        with self._probe_lock:
            stop_event = self._probe_stop_events.pop(provider_name, None)
            thread = self._probe_threads.pop(provider_name, None)

        if stop_event:
            stop_event.set()
        else:
            return  # no probe was active — skip logging

        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.PROBE_JOIN_TIMEOUT_SEC)
        logger.info("Stopped recovery probe for provider '%s'", provider_name)

    def _probe_loop(self, provider_name: str, stop_event: threading.Event) -> None:
        """Probe loop: wait PROBE_INTERVAL_SEC then test real connectivity (Story #619 HIGH-2).

        If a probe function is registered for provider_name, call it to determine
        success. Otherwise fall back to recording a synthetic success=True so the
        provider can recover from 'down' state even without a registered probe.
        """
        while not stop_event.is_set():
            stop_event.wait(timeout=self.PROBE_INTERVAL_SEC)
            if stop_event.is_set():
                break
            try:
                probe_fn = self._probe_functions.get(provider_name)
                if probe_fn:
                    try:
                        success = bool(probe_fn())  # type: ignore[operator]
                        logger.debug(
                            "Recovery probe for '%s': probe_fn returned %s",
                            provider_name,
                            success,
                        )
                    except Exception as probe_exc:
                        success = False
                        logger.debug(
                            "Recovery probe for '%s': probe_fn raised: %s",
                            provider_name,
                            probe_exc,
                            exc_info=True,
                        )
                else:
                    success = True  # synthetic fallback: no probe registered
                    logger.debug(
                        "Recovery probe for '%s': no probe registered, recorded synthetic success",
                        provider_name,
                    )
                self.record_call(
                    provider_name,
                    latency_ms=self.SYNTHETIC_PROBE_LATENCY_MS,
                    success=success,
                )
            except Exception as exc:
                logger.debug("Recovery probe for '%s' failed: %s", provider_name, exc)

    def get_health(
        self, provider: Optional[str] = None
    ) -> Dict[str, ProviderHealthStatus]:
        """Get health status for one or all providers."""
        with self._data_lock:
            if provider is not None:
                if provider not in self._metrics:
                    return {provider: self._empty_status(provider)}
                self._prune_old_metrics(provider)
                return {provider: self._compute_status(provider)}

            result = {}
            for pname in list(self._metrics.keys()):
                self._prune_old_metrics(pname)
                result[pname] = self._compute_status(pname)
            return result

    def get_best_provider(self, providers: List[str]) -> Optional[str]:
        """Get the provider with the best health score from the given list."""
        health = self.get_health()
        best_provider = None
        best_score = -1.0

        for p in providers:
            status = health.get(p)
            if status is not None and status.health_score > best_score:
                best_score = status.health_score
                best_provider = p

        return best_provider

    def _prune_old_metrics(self, provider: str) -> None:
        """Remove metrics older than rolling window. Must hold _data_lock."""
        cutoff = time.time() - (self._rolling_window_minutes * 60)
        metrics = self._metrics.get(provider)
        if metrics:
            while metrics and metrics[0].timestamp < cutoff:
                metrics.popleft()

    def _compute_status(self, provider: str) -> ProviderHealthStatus:
        """Compute health status from current metrics. Must hold _data_lock."""
        metrics = self._metrics.get(provider)
        if not metrics:
            return self._empty_status(provider)

        total = len(metrics)
        successful = sum(1 for m in metrics if m.success)
        failed = total - successful

        error_rate = failed / total if total > 0 else 0.0
        availability = successful / total if total > 0 else 1.0

        # Latency percentiles (only from successful calls)
        latencies = sorted(m.latency_ms for m in metrics if m.success)
        p50 = self._percentile(latencies, 50)
        p95 = self._percentile(latencies, 95)
        p99 = self._percentile(latencies, 99)

        # Health score: availability * (1 - latency_penalty)
        latency_penalty = min(1.0, p95 / (self._latency_p95_threshold_ms * 2))
        health_score = availability * (1.0 - latency_penalty * 0.5)
        health_score = max(0.0, min(1.0, health_score))

        # Status determination
        consecutive = self._consecutive_failures.get(provider, 0)
        if (
            error_rate > DEFAULT_DOWN_ERROR_RATE
            or consecutive >= DEFAULT_DOWN_CONSECUTIVE_FAILURES
        ):
            status = "down"
        elif (
            error_rate > self._error_rate_threshold
            or p95 > self._latency_p95_threshold_ms
            or availability < self._availability_threshold
        ):
            status = "degraded"
        else:
            status = "healthy"

        if status in ("degraded", "down"):
            logger.warning(
                "Provider %s health: %s (error_rate=%.2f, p95=%.0fms, availability=%.2f)",
                provider,
                status,
                error_rate,
                p95,
                availability,
            )

        # Bug #678: check sin-bin state without re-acquiring _data_lock (already held)
        is_sb = time.monotonic() < self._sinbin_until.get(provider, _SINBIN_NOT_ACTIVE)

        return ProviderHealthStatus(
            provider=provider,
            status=status,
            health_score=health_score,
            p50_latency_ms=p50,
            p95_latency_ms=p95,
            p99_latency_ms=p99,
            error_rate=error_rate,
            availability=availability,
            total_requests=total,
            successful_requests=successful,
            failed_requests=failed,
            window_minutes=self._rolling_window_minutes,
            sinbinned=is_sb,
        )

    def _empty_status(self, provider: str) -> ProviderHealthStatus:
        """Return empty status for a provider with no data."""
        return ProviderHealthStatus(
            provider=provider,
            status="healthy",
            health_score=1.0,
            p50_latency_ms=0.0,
            p95_latency_ms=0.0,
            p99_latency_ms=0.0,
            error_rate=0.0,
            availability=1.0,
            total_requests=0,
            successful_requests=0,
            failed_requests=0,
            window_minutes=self._rolling_window_minutes,
        )

    @staticmethod
    def _percentile(sorted_values: List[float], pct: int) -> float:
        """Calculate percentile using linear interpolation (numpy default).

        Bug #873: replaced floor-based nearest-rank with linear interpolation
        so p50/p95/p99 produce distinct values even for small N (which is the
        typical operating condition on a 60-minute rolling window).
        """
        if not sorted_values:
            return 0.0
        return float(np.percentile(sorted_values, pct))
