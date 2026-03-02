"""
Unit tests for inflection detection and escalation data structures in metrics.py

Story #334: Concurrency Escalation Tests with Degradation Detection
AC4: Degradation Inflection Point Detection

TDD: These tests were written BEFORE the implementation.
"""

from __future__ import annotations

import sys
import os

import pytest

# Add the perf-suite directory to path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite"))


def _make_metrics(scenario_name: str, p50: float, error_rate: float = 0.0) -> object:
    """Create a MetricsResult with specific p50 and error_rate for testing."""
    from metrics import MetricsResult

    return MetricsResult(
        scenario_name=scenario_name,
        p50_ms=p50,
        p95_ms=p50 * 1.5,
        p99_ms=p50 * 2.0,
        throughput_rps=10.0,
        error_rate_pct=error_rate,
        total_requests=20,
        total_errors=int(20 * error_rate / 100.0),
        raw_timings=[p50] * 20,
    )


class TestInflectionResultDataclass:
    """Tests for the InflectionResult dataclass."""

    def test_basic_construction_with_inflection(self):
        from metrics import InflectionResult

        result = InflectionResult(
            inflection_level=20,
            baseline_p50_ms=120.0,
            inflection_p50_ms=260.0,
            baseline_unreliable=False,
        )
        assert result.inflection_level == 20
        assert result.baseline_p50_ms == 120.0
        assert result.inflection_p50_ms == 260.0
        assert result.baseline_unreliable is False

    def test_stable_performance_no_inflection(self):
        from metrics import InflectionResult

        result = InflectionResult(
            inflection_level=None,
            baseline_p50_ms=120.0,
            inflection_p50_ms=None,
            baseline_unreliable=False,
        )
        assert result.inflection_level is None
        assert result.inflection_p50_ms is None

    def test_baseline_unreliable_flag(self):
        from metrics import InflectionResult

        result = InflectionResult(
            inflection_level=None,
            baseline_p50_ms=10.0,
            inflection_p50_ms=None,
            baseline_unreliable=True,
        )
        assert result.baseline_unreliable is True

    def test_to_dict_with_inflection(self):
        from metrics import InflectionResult

        result = InflectionResult(
            inflection_level=10,
            baseline_p50_ms=100.0,
            inflection_p50_ms=210.0,
            baseline_unreliable=False,
        )
        d = result.to_dict()
        assert d["inflection_level"] == 10
        assert d["baseline_p50_ms"] == 100.0
        assert d["inflection_p50_ms"] == 210.0
        assert d["baseline_unreliable"] is False

    def test_to_dict_without_inflection(self):
        from metrics import InflectionResult

        result = InflectionResult(
            inflection_level=None,
            baseline_p50_ms=100.0,
            inflection_p50_ms=None,
            baseline_unreliable=False,
        )
        d = result.to_dict()
        assert d["inflection_level"] is None
        assert d["inflection_p50_ms"] is None

    def test_to_dict_is_json_serializable(self):
        import json
        from metrics import InflectionResult

        result = InflectionResult(
            inflection_level=5,
            baseline_p50_ms=80.0,
            inflection_p50_ms=170.0,
            baseline_unreliable=False,
        )
        # Should not raise
        json.dumps(result.to_dict())


class TestEscalationResultDataclass:
    """Tests for the EscalationResult dataclass."""

    def test_basic_construction(self):
        from metrics import EscalationResult, InflectionResult, MetricsResult

        scenario = object()  # placeholder
        level_metrics = {
            1: _make_metrics("s", 100.0),
            2: _make_metrics("s", 110.0),
        }
        inflection = InflectionResult(
            inflection_level=None,
            baseline_p50_ms=100.0,
            inflection_p50_ms=None,
            baseline_unreliable=False,
        )

        result = EscalationResult(
            scenario=scenario,
            level_metrics=level_metrics,
            inflection=inflection,
        )

        assert result.level_metrics[1].p50_ms == 100.0
        assert result.level_metrics[2].p50_ms == 110.0
        assert result.inflection.inflection_level is None

    def test_to_dict_includes_levels_and_inflection(self):
        import json
        from metrics import EscalationResult, InflectionResult
        from config import Scenario

        scenario = Scenario(
            name="test_s",
            endpoint="search_code",
            protocol="mcp",
            method="POST",
            parameters={},
            repo_alias="repo-global",
            priority="highest",
        )
        level_metrics = {
            1: _make_metrics("test_s", 100.0),
            5: _make_metrics("test_s", 120.0),
        }
        inflection = InflectionResult(
            inflection_level=None,
            baseline_p50_ms=100.0,
            inflection_p50_ms=None,
            baseline_unreliable=False,
        )

        result = EscalationResult(
            scenario=scenario,
            level_metrics=level_metrics,
            inflection=inflection,
        )

        d = result.to_dict()
        assert "levels" in d
        assert "1" in d["levels"] or 1 in d["levels"]
        assert "inflection" in d
        # Must be JSON-serializable
        json.dumps(d)

    def test_to_dict_includes_endpoint_and_repo(self):
        from metrics import EscalationResult, InflectionResult
        from config import Scenario

        scenario = Scenario(
            name="semantic_search_tries",
            endpoint="search_code",
            protocol="mcp",
            method="POST",
            parameters={},
            repo_alias="tries-global",
            priority="highest",
        )
        inflection = InflectionResult(
            inflection_level=None,
            baseline_p50_ms=100.0,
            inflection_p50_ms=None,
            baseline_unreliable=False,
        )

        result = EscalationResult(
            scenario=scenario,
            level_metrics={1: _make_metrics("test", 100.0)},
            inflection=inflection,
        )

        d = result.to_dict()
        assert d["endpoint"] == "search_code"
        assert d["repo_alias"] == "tries-global"
        assert d["priority"] == "highest"


class TestDetectInflection:
    """Tests for detect_inflection() - finding the degradation inflection point."""

    def test_stable_performance_returns_null_inflection(self):
        """No inflection when p50 stays within 2x baseline at all levels."""
        from metrics import detect_inflection

        # p50 increases slowly - never exceeds 2x baseline (100ms)
        level_results = {
            1: _make_metrics("s", 100.0),
            2: _make_metrics("s", 120.0),
            5: _make_metrics("s", 150.0),
            10: _make_metrics("s", 180.0),
        }

        inflection = detect_inflection(level_results)
        assert inflection.inflection_level is None
        assert inflection.inflection_p50_ms is None
        assert inflection.baseline_unreliable is False

    def test_inflection_detected_at_first_crossing(self):
        """Inflection is the FIRST level where p50 > 2x baseline."""
        from metrics import detect_inflection

        # Baseline is 100ms. Inflection threshold = 200ms.
        # Level 5 crosses it.
        level_results = {
            1: _make_metrics("s", 100.0),
            2: _make_metrics("s", 130.0),
            5: _make_metrics("s", 210.0),   # first crossing: 210 > 200
            10: _make_metrics("s", 400.0),
        }

        inflection = detect_inflection(level_results)
        assert inflection.inflection_level == 5
        assert inflection.inflection_p50_ms == 210.0
        assert inflection.baseline_p50_ms == 100.0

    def test_inflection_at_highest_level(self):
        """Inflection can occur at the last level tested."""
        from metrics import detect_inflection

        level_results = {
            1: _make_metrics("s", 100.0),
            2: _make_metrics("s", 110.0),
            5: _make_metrics("s", 150.0),
            50: _make_metrics("s", 250.0),  # 250 > 2*100=200
        }

        inflection = detect_inflection(level_results)
        assert inflection.inflection_level == 50
        assert inflection.inflection_p50_ms == 250.0

    def test_exactly_2x_is_not_inflection(self):
        """p50 = exactly 2x baseline is NOT an inflection (must be strictly greater)."""
        from metrics import detect_inflection

        level_results = {
            1: _make_metrics("s", 100.0),
            2: _make_metrics("s", 200.0),  # Exactly 2x - NOT inflection
            5: _make_metrics("s", 199.0),
        }

        inflection = detect_inflection(level_results)
        assert inflection.inflection_level is None

    def test_baseline_unreliable_when_error_rate_high(self):
        """baseline_unreliable=True when baseline (level=1) error rate > 50%."""
        from metrics import detect_inflection

        level_results = {
            1: _make_metrics("s", 100.0, error_rate=60.0),  # 60% errors
            2: _make_metrics("s", 120.0),
        }

        inflection = detect_inflection(level_results)
        assert inflection.baseline_unreliable is True

    def test_baseline_reliable_when_error_rate_low(self):
        """baseline_unreliable=False when baseline error rate <= 50%."""
        from metrics import detect_inflection

        level_results = {
            1: _make_metrics("s", 100.0, error_rate=50.0),  # exactly 50% - reliable
            2: _make_metrics("s", 110.0),
        }

        inflection = detect_inflection(level_results)
        assert inflection.baseline_unreliable is False

    def test_baseline_is_lowest_level(self):
        """Baseline is always the lowest concurrency level (not necessarily 1)."""
        from metrics import detect_inflection

        # Levels start at 2, not 1
        level_results = {
            2: _make_metrics("s", 80.0),   # baseline = 80ms, threshold = 160ms
            5: _make_metrics("s", 100.0),
            10: _make_metrics("s", 170.0),  # 170 > 160 -> inflection
        }

        inflection = detect_inflection(level_results)
        assert inflection.baseline_p50_ms == 80.0
        assert inflection.inflection_level == 10

    def test_single_level_no_inflection(self):
        """With only one level, there can be no inflection."""
        from metrics import detect_inflection

        level_results = {
            1: _make_metrics("s", 100.0),
        }

        inflection = detect_inflection(level_results)
        assert inflection.inflection_level is None
        assert inflection.baseline_p50_ms == 100.0
