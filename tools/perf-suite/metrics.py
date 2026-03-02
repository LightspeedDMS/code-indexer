"""
Metrics calculation for the CIDX performance test harness.

Story #333: Performance Test Harness with Single-User Baselines
AC5: p50/p95/p99 percentile calculation, throughput, error rate, edge cases.

Story #334: Concurrency Escalation Tests with Degradation Detection
AC4: InflectionResult, EscalationResult, detect_inflection() - see escalation_types.py.
     Re-exported here for backward compatibility.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Re-export escalation types from their dedicated module (split for Anti-File-Bloat).
# Importers can use either `from metrics import InflectionResult` or
# `from escalation_types import InflectionResult` - both work.
from escalation_types import (  # noqa: F401
    BASELINE_UNRELIABLE_ERROR_RATE_PCT,
    DEGRADATION_MULTIPLIER,
    EscalationResult,
    InflectionResult,
    detect_inflection,
)


@dataclass
class RequestResult:
    """Result of a single HTTP request execution."""

    response_time_ms: float
    status_code: int
    success: bool
    response_size_bytes: int
    error_message: Optional[str] = None


@dataclass
class MetricsResult:
    """Aggregated metrics for a single scenario."""

    scenario_name: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    throughput_rps: float
    error_rate_pct: float
    total_requests: int
    total_errors: int
    raw_timings: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to a JSON-serializable dict."""
        return {
            "scenario_name": self.scenario_name,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "throughput_rps": self.throughput_rps,
            "error_rate_pct": self.error_rate_pct,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "raw_timings": self.raw_timings,
        }


def calculate_percentile(timings: list[float], percentile: int) -> float:
    """
    Calculate a percentile value from a list of timings using the sorted array method.

    Args:
        timings: List of response times in milliseconds.
        percentile: Integer percentile to calculate (0-100).

    Returns:
        The percentile value.

    Raises:
        ValueError: If timings is empty or percentile is out of range.
    """
    if not timings:
        raise ValueError("Cannot calculate percentile on empty list")
    if percentile < 0 or percentile > 100:
        raise ValueError(f"Percentile must be between 0 and 100, got {percentile}")

    sorted_timings = sorted(timings)
    n = len(sorted_timings)

    # Use ceiling-based index: index = ceil(p/100 * n) - 1
    index = math.ceil(percentile / 100.0 * n) - 1
    index = max(0, min(index, n - 1))

    return sorted_timings[index]


def calculate_throughput(total_requests: int, total_elapsed_seconds: float) -> float:
    """
    Calculate throughput in requests per second.

    Args:
        total_requests: Number of requests executed.
        total_elapsed_seconds: Wall clock time elapsed.

    Returns:
        Requests per second. Returns 0.0 if elapsed is zero or requests is zero.
    """
    if total_requests == 0 or total_elapsed_seconds == 0.0:
        return 0.0
    return total_requests / total_elapsed_seconds


def calculate_error_rate(error_count: int, total_requests: int) -> float:
    """
    Calculate error rate as a percentage.

    Args:
        error_count: Number of failed requests.
        total_requests: Total number of requests executed.

    Returns:
        Error percentage (0.0 to 100.0). Returns 0.0 if total_requests is zero.
    """
    if total_requests == 0:
        return 0.0
    return (error_count / total_requests) * 100.0


def aggregate_metrics(
    scenario_name: str,
    results: list[RequestResult],
    total_elapsed_seconds: float,
) -> MetricsResult:
    """
    Aggregate a list of RequestResult into a MetricsResult.

    Percentiles are calculated over all timings (including error requests).
    Throughput uses only total_requests / total_elapsed_seconds.

    Args:
        scenario_name: Human-readable name of the scenario.
        results: List of individual request results.
        total_elapsed_seconds: Total wall clock time for all requests.

    Returns:
        Aggregated MetricsResult.
    """
    total_requests = len(results)
    total_errors = sum(1 for r in results if not r.success)
    raw_timings = [r.response_time_ms for r in results]

    if raw_timings:
        p50 = calculate_percentile(raw_timings, 50)
        p95 = calculate_percentile(raw_timings, 95)
        p99 = calculate_percentile(raw_timings, 99)
    else:
        p50 = p95 = p99 = 0.0

    throughput = calculate_throughput(total_requests, total_elapsed_seconds)
    error_rate = calculate_error_rate(total_errors, total_requests)

    return MetricsResult(
        scenario_name=scenario_name,
        p50_ms=p50,
        p95_ms=p95,
        p99_ms=p99,
        throughput_rps=throughput,
        error_rate_pct=error_rate,
        total_requests=total_requests,
        total_errors=total_errors,
        raw_timings=raw_timings,
    )
