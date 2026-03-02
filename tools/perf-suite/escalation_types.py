"""
Escalation data structures and inflection detection for the CIDX perf test harness.

Story #334: Concurrency Escalation Tests with Degradation Detection
AC4: InflectionResult, EscalationResult, detect_inflection()

Extracted from metrics.py to keep each module under 200 lines (Anti-File-Bloat).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from config import Scenario
    from metrics import MetricsResult

# Error rate threshold above which the baseline is considered unreliable
BASELINE_UNRELIABLE_ERROR_RATE_PCT = 50.0

# Degradation multiplier: inflection = first level where p50 > this * baseline p50
DEGRADATION_MULTIPLIER = 2.0


@dataclass
class InflectionResult:
    """Degradation inflection point detected across concurrency levels."""

    inflection_level: Optional[int]  # None = stable, no inflection detected
    baseline_p50_ms: float
    inflection_p50_ms: Optional[float]  # None when no inflection
    baseline_unreliable: bool  # True if baseline error rate > 50%

    def to_dict(self) -> dict:
        """Convert to a JSON-serializable dict."""
        return {
            "inflection_level": self.inflection_level,
            "baseline_p50_ms": self.baseline_p50_ms,
            "inflection_p50_ms": self.inflection_p50_ms,
            "baseline_unreliable": self.baseline_unreliable,
        }


@dataclass
class EscalationResult:
    """Per-scenario result across all concurrency levels."""

    scenario: Scenario
    level_metrics: dict[int, MetricsResult]  # concurrency_level -> metrics
    inflection: InflectionResult

    def to_dict(self) -> dict:
        """Convert to a JSON-serializable dict compatible with Story #334 output schema."""
        levels: dict[str, Any] = {
            str(level): metrics.to_dict()
            for level, metrics in sorted(self.level_metrics.items())
        }
        return {
            "endpoint": self.scenario.endpoint,
            "repo_alias": self.scenario.repo_alias,
            "priority": self.scenario.priority,
            "levels": levels,
            "inflection": self.inflection.to_dict(),
        }


def detect_inflection(level_results: dict[int, MetricsResult]) -> InflectionResult:
    """
    Find the first concurrency level where p50 exceeds 2x the baseline p50.

    Baseline = the lowest concurrency level in the dict.
    Inflection = first level (ascending) where p50 > 2 * baseline_p50.
    Stable performance reports inflection_level = None.

    Args:
        level_results: Dict mapping concurrency level -> MetricsResult.

    Returns:
        InflectionResult with inflection_level, baseline info, and reliability flag.
    """
    if not level_results:
        return InflectionResult(
            inflection_level=None,
            baseline_p50_ms=0.0,
            inflection_p50_ms=None,
            baseline_unreliable=False,
        )

    sorted_levels = sorted(level_results.keys())
    baseline_level = sorted_levels[0]
    baseline_metrics = level_results[baseline_level]
    baseline_p50 = baseline_metrics.p50_ms
    threshold = baseline_p50 * DEGRADATION_MULTIPLIER
    baseline_unreliable = baseline_metrics.error_rate_pct > BASELINE_UNRELIABLE_ERROR_RATE_PCT

    # Check levels above baseline (skip baseline itself)
    for level in sorted_levels[1:]:
        level_p50 = level_results[level].p50_ms
        if level_p50 > threshold:
            return InflectionResult(
                inflection_level=level,
                baseline_p50_ms=baseline_p50,
                inflection_p50_ms=level_p50,
                baseline_unreliable=baseline_unreliable,
            )

    return InflectionResult(
        inflection_level=None,
        baseline_p50_ms=baseline_p50,
        inflection_p50_ms=None,
        baseline_unreliable=baseline_unreliable,
    )
