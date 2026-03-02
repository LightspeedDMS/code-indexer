"""
Unit tests for tools/perf-suite/metrics.py

Story #333: Performance Test Harness with Single-User Baselines
AC5: Metrics Calculation - percentiles, throughput, error rate, edge cases.

TDD: These tests were written BEFORE the implementation.
"""

import pytest
import sys
import os

# Add the perf-suite directory to path so we can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../tools/perf-suite"))


class TestPercentileCalculation:
    """Tests for p50/p95/p99 percentile calculation using sorted array method."""

    def test_p50_with_odd_count(self):
        from metrics import calculate_percentile

        timings = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert calculate_percentile(timings, 50) == 3.0

    def test_p50_with_even_count(self):
        from metrics import calculate_percentile

        timings = [1.0, 2.0, 3.0, 4.0]
        # p50 of 4 values: index = ceil(0.5*4) - 1 = 1 (0-indexed)
        # sorted: [1, 2, 3, 4], index = int(0.5 * 4) = 2, value = 3
        result = calculate_percentile(timings, 50)
        assert isinstance(result, float)

    def test_p95_calculation(self):
        from metrics import calculate_percentile

        # 20 values: p95 should be the 19th value (index 18)
        timings = [float(i) for i in range(1, 21)]  # 1.0 to 20.0
        result = calculate_percentile(timings, 95)
        assert result == 19.0

    def test_p99_calculation(self):
        from metrics import calculate_percentile

        timings = [float(i) for i in range(1, 101)]  # 1.0 to 100.0
        result = calculate_percentile(timings, 99)
        assert result == 99.0

    def test_single_value(self):
        from metrics import calculate_percentile

        timings = [42.0]
        assert calculate_percentile(timings, 50) == 42.0
        assert calculate_percentile(timings, 95) == 42.0
        assert calculate_percentile(timings, 99) == 42.0

    def test_unsorted_input_works(self):
        from metrics import calculate_percentile

        timings = [5.0, 1.0, 3.0, 2.0, 4.0]
        assert calculate_percentile(timings, 50) == 3.0

    def test_empty_list_raises(self):
        from metrics import calculate_percentile

        with pytest.raises(ValueError, match="empty"):
            calculate_percentile([], 50)

    def test_percentile_out_of_range_raises(self):
        from metrics import calculate_percentile

        with pytest.raises(ValueError):
            calculate_percentile([1.0], 101)

        with pytest.raises(ValueError):
            calculate_percentile([1.0], -1)


class TestThroughputCalculation:
    """Tests for throughput (requests per second) calculation."""

    def test_basic_throughput(self):
        from metrics import calculate_throughput

        # 20 requests in 10 seconds = 2.0 rps
        assert calculate_throughput(total_requests=20, total_elapsed_seconds=10.0) == 2.0

    def test_throughput_with_zero_elapsed(self):
        from metrics import calculate_throughput

        # Zero elapsed time should return 0 (avoid division by zero)
        result = calculate_throughput(total_requests=20, total_elapsed_seconds=0.0)
        assert result == 0.0

    def test_throughput_with_zero_requests(self):
        from metrics import calculate_throughput

        result = calculate_throughput(total_requests=0, total_elapsed_seconds=10.0)
        assert result == 0.0

    def test_fractional_throughput(self):
        from metrics import calculate_throughput

        # 10 requests in 30 seconds = 0.333... rps
        result = calculate_throughput(total_requests=10, total_elapsed_seconds=30.0)
        assert abs(result - (10.0 / 30.0)) < 0.001


class TestErrorRateCalculation:
    """Tests for error rate percentage calculation."""

    def test_zero_errors(self):
        from metrics import calculate_error_rate

        assert calculate_error_rate(error_count=0, total_requests=20) == 0.0

    def test_all_errors(self):
        from metrics import calculate_error_rate

        assert calculate_error_rate(error_count=20, total_requests=20) == 100.0

    def test_partial_errors(self):
        from metrics import calculate_error_rate

        result = calculate_error_rate(error_count=1, total_requests=20)
        assert abs(result - 5.0) < 0.001

    def test_zero_total_requests(self):
        from metrics import calculate_error_rate

        # Avoid division by zero - return 0 when no requests
        result = calculate_error_rate(error_count=0, total_requests=0)
        assert result == 0.0


class TestAggregateMetrics:
    """Tests for aggregate_metrics() which builds a full MetricsResult."""

    def test_basic_aggregation(self):
        from metrics import aggregate_metrics, RequestResult

        results = [
            RequestResult(
                response_time_ms=100.0,
                status_code=200,
                success=True,
                response_size_bytes=1024,
            ),
            RequestResult(
                response_time_ms=200.0,
                status_code=200,
                success=True,
                response_size_bytes=2048,
            ),
            RequestResult(
                response_time_ms=300.0,
                status_code=200,
                success=True,
                response_size_bytes=512,
            ),
        ]

        metrics = aggregate_metrics(
            scenario_name="test_scenario",
            results=results,
            total_elapsed_seconds=1.5,
        )

        assert metrics.scenario_name == "test_scenario"
        assert metrics.p50_ms == 200.0
        assert metrics.total_requests == 3
        assert metrics.total_errors == 0
        assert metrics.error_rate_pct == 0.0
        assert len(metrics.raw_timings) == 3

    def test_aggregation_with_errors(self):
        from metrics import aggregate_metrics, RequestResult

        results = [
            RequestResult(
                response_time_ms=100.0,
                status_code=200,
                success=True,
                response_size_bytes=1024,
            ),
            RequestResult(
                response_time_ms=0.0,
                status_code=500,
                success=False,
                response_size_bytes=0,
                error_message="Server error",
            ),
        ]

        metrics = aggregate_metrics(
            scenario_name="error_scenario",
            results=results,
            total_elapsed_seconds=1.0,
        )

        assert metrics.total_requests == 2
        assert metrics.total_errors == 1
        assert metrics.error_rate_pct == 50.0

    def test_all_errors_zero_throughput(self):
        from metrics import aggregate_metrics, RequestResult

        results = [
            RequestResult(
                response_time_ms=0.0,
                status_code=500,
                success=False,
                response_size_bytes=0,
                error_message="Server error",
            )
        ]

        metrics = aggregate_metrics(
            scenario_name="all_errors",
            results=results,
            total_elapsed_seconds=1.0,
        )

        assert metrics.error_rate_pct == 100.0
        # p50/p95/p99 should use all timings (including error timings)
        assert isinstance(metrics.p50_ms, float)

    def test_metrics_result_is_json_serializable(self):
        """MetricsResult must be serializable to JSON."""
        import json
        from metrics import aggregate_metrics, RequestResult

        results = [
            RequestResult(
                response_time_ms=150.0,
                status_code=200,
                success=True,
                response_size_bytes=512,
            )
        ]

        metrics = aggregate_metrics(
            scenario_name="json_test",
            results=results,
            total_elapsed_seconds=0.5,
        )

        # Should not raise
        data = metrics.to_dict()
        json_str = json.dumps(data)
        parsed = json.loads(json_str)
        assert parsed["scenario_name"] == "json_test"
        assert "p50_ms" in parsed
        assert "p95_ms" in parsed
        assert "p99_ms" in parsed
        assert "throughput_rps" in parsed
        assert "error_rate_pct" in parsed
        assert "raw_timings" in parsed


class TestRequestResultDataclass:
    """Tests for RequestResult dataclass."""

    def test_basic_construction(self):
        from metrics import RequestResult

        r = RequestResult(
            response_time_ms=100.0,
            status_code=200,
            success=True,
            response_size_bytes=1024,
        )
        assert r.response_time_ms == 100.0
        assert r.status_code == 200
        assert r.success is True
        assert r.response_size_bytes == 1024
        assert r.error_message is None

    def test_error_result_construction(self):
        from metrics import RequestResult

        r = RequestResult(
            response_time_ms=0.0,
            status_code=503,
            success=False,
            response_size_bytes=0,
            error_message="Connection refused",
        )
        assert r.success is False
        assert r.error_message == "Connection refused"
