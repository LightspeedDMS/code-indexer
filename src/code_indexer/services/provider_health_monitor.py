"""Provider health monitoring service (Story #491).

Tracks per-provider embedding API metrics in a rolling window.
Thread-safe singleton for use across query and indexing operations.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

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
    status: str  # "healthy", "degraded", "down"
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


# Default thresholds
DEFAULT_ERROR_RATE_THRESHOLD = 0.1  # 10%
DEFAULT_LATENCY_P95_THRESHOLD_MS = 5000.0  # 5 seconds
DEFAULT_AVAILABILITY_THRESHOLD = 0.95  # 95%
DEFAULT_ROLLING_WINDOW_MINUTES = 60
DEFAULT_DOWN_ERROR_RATE = 0.5  # 50% error rate = down
DEFAULT_DOWN_CONSECUTIVE_FAILURES = 5


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

    def record_call(self, provider: str, latency_ms: float, success: bool) -> None:
        """Record an API call metric. Thread-safe."""
        metric = HealthMetric(
            timestamp=time.time(),
            latency_ms=latency_ms,
            success=success,
            provider=provider,
        )

        with self._data_lock:
            if provider not in self._metrics:
                self._metrics[provider] = deque()
            self._metrics[provider].append(metric)

            # Track consecutive failures
            if success:
                self._consecutive_failures[provider] = 0
            else:
                self._consecutive_failures[provider] = (
                    self._consecutive_failures.get(provider, 0) + 1
                )

            # Prune old entries
            self._prune_old_metrics(provider)

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
        """Calculate percentile from sorted values using floor-based index."""
        if not sorted_values:
            return 0.0
        idx = int(len(sorted_values) * pct / 100)
        idx = min(idx, len(sorted_values) - 1)
        return sorted_values[idx]
