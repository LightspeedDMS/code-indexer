"""Tests for ProviderHealthMonitor service (Story #491).

TDD red phase: these tests are written before the implementation exists.
All tests use ProviderHealthMonitor.reset_instance() in setUp for clean state.
"""

import threading
import time
import unittest

from code_indexer.services.provider_health_monitor import (
    DEFAULT_DOWN_CONSECUTIVE_FAILURES,
    DEFAULT_DOWN_ERROR_RATE,
    DEFAULT_ERROR_RATE_THRESHOLD,
    DEFAULT_LATENCY_P95_THRESHOLD_MS,
    DEFAULT_ROLLING_WINDOW_MINUTES,
    HealthMetric,
    ProviderHealthMonitor,
    ProviderHealthStatus,
)


class TestProviderHealthMonitor(unittest.TestCase):
    """Tests for ProviderHealthMonitor."""

    def setUp(self) -> None:
        """Reset singleton before each test to ensure clean state."""
        ProviderHealthMonitor.reset_instance()

    def tearDown(self) -> None:
        """Reset singleton after each test to avoid state leakage."""
        ProviderHealthMonitor.reset_instance()

    # -------------------------------------------------------------------------
    # Test 1: record_call_stores_metric
    # -------------------------------------------------------------------------

    def test_record_call_stores_metric(self) -> None:
        """Recording a call should result in get_health returning data for that provider."""
        monitor = ProviderHealthMonitor()
        monitor.record_call("voyage", latency_ms=100.0, success=True)

        health = monitor.get_health("voyage")
        assert "voyage" in health
        status = health["voyage"]
        assert status.total_requests == 1
        assert status.successful_requests == 1
        assert status.failed_requests == 0

    # -------------------------------------------------------------------------
    # Test 2: health_score_healthy
    # -------------------------------------------------------------------------

    def test_health_score_healthy(self) -> None:
        """All successful fast calls should yield healthy status and score near 1.0."""
        monitor = ProviderHealthMonitor()
        for _ in range(20):
            monitor.record_call("voyage", latency_ms=100.0, success=True)

        health = monitor.get_health("voyage")
        status = health["voyage"]
        assert status.status == "healthy"
        assert status.health_score > 0.8
        assert status.error_rate == 0.0
        assert status.availability == 1.0

    # -------------------------------------------------------------------------
    # Test 3: health_score_degraded
    # -------------------------------------------------------------------------

    def test_health_score_degraded(self) -> None:
        """Error rate > 10% should yield degraded status."""
        monitor = ProviderHealthMonitor()
        # Interleave: 4 successes then 1 failure, repeated 20 times.
        # Result: 80 successes + 20 failures = 20% error rate (above 10% threshold).
        # Consecutive failures never exceed 1, so the DOWN threshold (5) is not reached.
        for _ in range(20):
            for _ in range(4):
                monitor.record_call("cohere", latency_ms=200.0, success=True)
            monitor.record_call("cohere", latency_ms=0.0, success=False)

        health = monitor.get_health("cohere")
        status = health["cohere"]
        assert status.status == "degraded"
        assert status.error_rate == 0.20
        assert status.total_requests == 100

    # -------------------------------------------------------------------------
    # Test 4: health_score_down
    # -------------------------------------------------------------------------

    def test_health_score_down(self) -> None:
        """Error rate > 50% should yield down status."""
        monitor = ProviderHealthMonitor()
        # 40 successful, 60 failed = 60% error rate (above 50% threshold)
        for _ in range(40):
            monitor.record_call("openai", latency_ms=150.0, success=True)
        for _ in range(60):
            monitor.record_call("openai", latency_ms=0.0, success=False)

        health = monitor.get_health("openai")
        status = health["openai"]
        assert status.status == "down"
        assert status.error_rate > DEFAULT_DOWN_ERROR_RATE

    # -------------------------------------------------------------------------
    # Test 5: consecutive_failures_down
    # -------------------------------------------------------------------------

    def test_consecutive_failures_down(self) -> None:
        """5 consecutive failures should yield down status."""
        monitor = ProviderHealthMonitor()
        # First some successes to avoid high error rate triggering down independently
        for _ in range(100):
            monitor.record_call("voyage", latency_ms=100.0, success=True)
        # Then exactly DEFAULT_DOWN_CONSECUTIVE_FAILURES failures in a row
        for _ in range(DEFAULT_DOWN_CONSECUTIVE_FAILURES):
            monitor.record_call("voyage", latency_ms=0.0, success=False)

        health = monitor.get_health("voyage")
        status = health["voyage"]
        assert status.status == "down"

    # -------------------------------------------------------------------------
    # Test 6: latency_percentiles
    # -------------------------------------------------------------------------

    def test_latency_percentiles(self) -> None:
        """Verify p50/p95/p99 calculation matches floor-based index algorithm."""
        monitor = ProviderHealthMonitor()
        # Record 10 successful calls with latencies 10, 20, ..., 100 ms
        latencies = [float(i * 10) for i in range(1, 11)]  # [10, 20, ..., 100]
        for lat in latencies:
            monitor.record_call("voyage", latency_ms=lat, success=True)

        health = monitor.get_health("voyage")
        status = health["voyage"]

        # Floor-based percentile: idx = int(10 * pct / 100), clamped to 0..9
        # p50: idx = int(10 * 50 / 100) = 5 → sorted[5] = 60
        # p95: idx = int(10 * 95 / 100) = 9 → sorted[9] = 100
        # p99: idx = int(10 * 99 / 100) = 9 → sorted[9] = 100 (clamped)
        assert status.p50_latency_ms == 60.0
        assert status.p95_latency_ms == 100.0
        assert status.p99_latency_ms == 100.0

    # -------------------------------------------------------------------------
    # Test 7: rolling_window_prunes_old
    # -------------------------------------------------------------------------

    def test_rolling_window_prunes_old(self) -> None:
        """Metrics older than the rolling window should be pruned."""
        monitor = ProviderHealthMonitor(rolling_window_minutes=60)

        # Record 5 calls
        for _ in range(5):
            monitor.record_call("voyage", latency_ms=100.0, success=True)

        # Manually age the metrics beyond the window
        cutoff_time = time.time() - (61 * 60)  # 61 minutes ago
        with monitor._data_lock:
            for metric in monitor._metrics["voyage"]:
                metric.timestamp = cutoff_time

        # Record 2 fresh calls
        monitor.record_call("voyage", latency_ms=50.0, success=True)
        monitor.record_call("voyage", latency_ms=60.0, success=True)

        # get_health triggers pruning
        health = monitor.get_health("voyage")
        status = health["voyage"]
        # Only the 2 fresh calls should remain
        assert status.total_requests == 2

    # -------------------------------------------------------------------------
    # Test 8: get_health_all_providers
    # -------------------------------------------------------------------------

    def test_get_health_all_providers(self) -> None:
        """Multiple providers should be tracked separately."""
        monitor = ProviderHealthMonitor()
        monitor.record_call("voyage", latency_ms=100.0, success=True)
        monitor.record_call("voyage", latency_ms=110.0, success=True)
        monitor.record_call("cohere", latency_ms=200.0, success=True)
        monitor.record_call("cohere", latency_ms=0.0, success=False)

        health = monitor.get_health()
        assert "voyage" in health
        assert "cohere" in health
        assert health["voyage"].total_requests == 2
        assert health["cohere"].total_requests == 2
        assert health["voyage"].failed_requests == 0
        assert health["cohere"].failed_requests == 1

    # -------------------------------------------------------------------------
    # Test 9: get_health_unknown_provider
    # -------------------------------------------------------------------------

    def test_get_health_unknown_provider(self) -> None:
        """Requesting health for a provider with no data returns empty/healthy status."""
        monitor = ProviderHealthMonitor()

        health = monitor.get_health("nonexistent_provider")
        assert "nonexistent_provider" in health
        status = health["nonexistent_provider"]
        assert status.status == "healthy"
        assert status.health_score == 1.0
        assert status.total_requests == 0
        assert status.successful_requests == 0
        assert status.failed_requests == 0
        assert status.error_rate == 0.0
        assert status.availability == 1.0

    # -------------------------------------------------------------------------
    # Test 10: get_best_provider
    # -------------------------------------------------------------------------

    def test_get_best_provider(self) -> None:
        """get_best_provider should return the provider with the highest health score."""
        monitor = ProviderHealthMonitor()
        # voyage: all success → high score
        for _ in range(20):
            monitor.record_call("voyage", latency_ms=100.0, success=True)
        # cohere: many failures → low score
        for _ in range(10):
            monitor.record_call("cohere", latency_ms=100.0, success=True)
        for _ in range(10):
            monitor.record_call("cohere", latency_ms=0.0, success=False)

        best = monitor.get_best_provider(["voyage", "cohere"])
        assert best == "voyage"

    def test_get_best_provider_unknown_providers(self) -> None:
        """get_best_provider returns None when no recorded calls for any provider."""
        monitor = ProviderHealthMonitor()
        result = monitor.get_best_provider(["never_recorded"])
        assert result is None

    def test_get_best_provider_empty_list(self) -> None:
        """get_best_provider with empty list returns None."""
        monitor = ProviderHealthMonitor()
        result = monitor.get_best_provider([])
        assert result is None

    # -------------------------------------------------------------------------
    # Test 11: thread_safety
    # -------------------------------------------------------------------------

    def test_thread_safety(self) -> None:
        """Concurrent record_call from multiple threads should not corrupt data."""
        monitor = ProviderHealthMonitor()
        num_threads = 20
        calls_per_thread = 50
        barrier = threading.Barrier(num_threads)
        errors: list = []

        def worker() -> None:
            try:
                barrier.wait()  # All threads start simultaneously
                for i in range(calls_per_thread):
                    monitor.record_call(
                        "voyage",
                        latency_ms=float(i * 2 + 1),
                        success=(i % 3 != 0),  # ~66% success
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread errors: {errors}"

        health = monitor.get_health("voyage")
        status = health["voyage"]
        # Total requests must equal num_threads * calls_per_thread
        assert status.total_requests == num_threads * calls_per_thread

    # -------------------------------------------------------------------------
    # Test 12: singleton_pattern
    # -------------------------------------------------------------------------

    def test_singleton_pattern(self) -> None:
        """get_instance should return the same object on repeated calls."""
        instance_a = ProviderHealthMonitor.get_instance()
        instance_b = ProviderHealthMonitor.get_instance()
        assert instance_a is instance_b

    def test_singleton_reset(self) -> None:
        """reset_instance should allow a new instance to be created."""
        instance_a = ProviderHealthMonitor.get_instance()
        ProviderHealthMonitor.reset_instance()
        instance_b = ProviderHealthMonitor.get_instance()
        assert instance_a is not instance_b

    def test_singleton_get_instance_kwargs_applied(self) -> None:
        """get_instance with kwargs configures the singleton on first creation."""
        monitor = ProviderHealthMonitor.get_instance(rolling_window_minutes=30)
        assert monitor._rolling_window_minutes == 30

    # -------------------------------------------------------------------------
    # Test 13: error_rate_calculation
    # -------------------------------------------------------------------------

    def test_error_rate_calculation(self) -> None:
        """Verify correct error rate: failed / total."""
        monitor = ProviderHealthMonitor()
        # 7 successes, 3 failures → error_rate = 0.3
        for _ in range(7):
            monitor.record_call("voyage", latency_ms=50.0, success=True)
        for _ in range(3):
            monitor.record_call("voyage", latency_ms=0.0, success=False)

        health = monitor.get_health("voyage")
        status = health["voyage"]
        assert status.total_requests == 10
        assert status.successful_requests == 7
        assert status.failed_requests == 3
        assert abs(status.error_rate - 0.3) < 1e-9

    # -------------------------------------------------------------------------
    # Additional edge case tests
    # -------------------------------------------------------------------------

    def test_empty_provider_list_get_health_all(self) -> None:
        """get_health() with no recorded calls should return empty dict."""
        monitor = ProviderHealthMonitor()
        health = monitor.get_health()
        assert health == {}

    def test_health_score_high_latency_degraded(self) -> None:
        """p95 latency above threshold should yield degraded status even with 100% success."""
        monitor = ProviderHealthMonitor(latency_p95_threshold_ms=1000.0)
        # All successful but very slow (10 seconds each)
        for _ in range(20):
            monitor.record_call("voyage", latency_ms=10000.0, success=True)

        health = monitor.get_health("voyage")
        status = health["voyage"]
        assert status.status == "degraded"

    def test_consecutive_failures_reset_on_success(self) -> None:
        """A success after consecutive failures should reset the failure counter."""
        monitor = ProviderHealthMonitor()
        # Record 4 consecutive failures (one below threshold)
        for _ in range(DEFAULT_DOWN_CONSECUTIVE_FAILURES - 1):
            monitor.record_call("voyage", latency_ms=0.0, success=False)
        # One success resets counter
        monitor.record_call("voyage", latency_ms=100.0, success=True)
        # Then 4 more failures (still below threshold of 5 consecutive)
        for _ in range(DEFAULT_DOWN_CONSECUTIVE_FAILURES - 1):
            monitor.record_call("voyage", latency_ms=0.0, success=False)

        with monitor._data_lock:
            consecutive = monitor._consecutive_failures.get("voyage", 0)
        assert consecutive == DEFAULT_DOWN_CONSECUTIVE_FAILURES - 1

    def test_percentile_single_value(self) -> None:
        """Percentile of a single-value list returns that value."""
        result = ProviderHealthMonitor._percentile([42.0], 50)
        assert result == 42.0

    def test_percentile_empty_list(self) -> None:
        """Percentile of empty list returns 0.0."""
        result = ProviderHealthMonitor._percentile([], 95)
        assert result == 0.0

    def test_dataclass_health_metric_fields(self) -> None:
        """HealthMetric dataclass should have required fields."""
        m = HealthMetric(
            timestamp=1.0, latency_ms=100.0, success=True, provider="voyage"
        )
        assert m.timestamp == 1.0
        assert m.latency_ms == 100.0
        assert m.success is True
        assert m.provider == "voyage"

    def test_dataclass_provider_health_status_fields(self) -> None:
        """ProviderHealthStatus dataclass should have all required fields."""
        s = ProviderHealthStatus(
            provider="voyage",
            status="healthy",
            health_score=1.0,
            p50_latency_ms=50.0,
            p95_latency_ms=95.0,
            p99_latency_ms=99.0,
            error_rate=0.0,
            availability=1.0,
            total_requests=10,
            successful_requests=10,
            failed_requests=0,
            window_minutes=60,
        )
        assert s.provider == "voyage"
        assert s.window_minutes == 60

    def test_constants_exported(self) -> None:
        """Module-level constants should be importable with expected values."""
        assert DEFAULT_ERROR_RATE_THRESHOLD == 0.1
        assert DEFAULT_LATENCY_P95_THRESHOLD_MS == 5000.0
        assert DEFAULT_ROLLING_WINDOW_MINUTES == 60
        assert DEFAULT_DOWN_ERROR_RATE == 0.5
        assert DEFAULT_DOWN_CONSECUTIVE_FAILURES == 5


if __name__ == "__main__":
    unittest.main()
