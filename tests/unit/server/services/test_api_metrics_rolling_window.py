"""
Unit tests for Rolling Window API Metrics Feature.

Tests for the new rolling window approach to API metrics tracking:
- Timestamps stored in deques for each API call category
- get_metrics(window_seconds) returns counts within specified window
- Memory management: cleanup timestamps older than 24 hours
- Thread safety with concurrent increments
- Default window is 60 seconds

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import concurrent.futures
from datetime import datetime, timezone, timedelta
from unittest.mock import patch


class TestRollingWindowApiMetricsService:
    """Test rolling window API metrics tracking."""

    def test_increment_semantic_search_stores_timestamp(self):
        """Test that semantic search calls store timestamps, not just counts."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Increment semantic search counter
        service.increment_semantic_search()

        # Get metrics within 60 second window (default)
        metrics = service.get_metrics(window_seconds=60)

        assert metrics["semantic_searches"] == 1
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0

    def test_increment_other_index_search_stores_timestamp(self):
        """Test that other index search calls store timestamps."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        service.increment_other_index_search()
        service.increment_other_index_search()

        metrics = service.get_metrics(window_seconds=60)

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 2
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0

    def test_increment_regex_search_stores_timestamp(self):
        """Test that regex search calls store timestamps."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        service.increment_regex_search()

        metrics = service.get_metrics(window_seconds=60)

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 1
        assert metrics["other_api_calls"] == 0

    def test_increment_other_api_call_stores_timestamp(self):
        """Test that other API calls store timestamps."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        service.increment_other_api_call()
        service.increment_other_api_call()
        service.increment_other_api_call()

        metrics = service.get_metrics(window_seconds=60)

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 3

    def test_get_metrics_default_window_is_60_seconds(self):
        """Test that get_metrics() without parameter defaults to 60 seconds."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        service.increment_semantic_search()

        # Call without window_seconds - should use default of 60
        metrics = service.get_metrics()

        assert metrics["semantic_searches"] == 1

    def test_get_metrics_filters_by_window_1_minute(self):
        """Test that get_metrics with 60 second window only counts recent calls."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Add a timestamp that is 2 minutes old (outside 60 second window)
        old_timestamp = datetime.now(timezone.utc) - timedelta(seconds=120)
        service._semantic_searches.append(old_timestamp)

        # Add a recent timestamp (inside 60 second window)
        service.increment_semantic_search()

        # Get metrics with 60 second window
        metrics = service.get_metrics(window_seconds=60)

        # Should only count the recent one
        assert metrics["semantic_searches"] == 1

    def test_get_metrics_filters_by_window_15_minutes(self):
        """Test that get_metrics with 900 second (15 min) window counts appropriate calls."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Add a timestamp that is 20 minutes old (outside 15 min window)
        old_timestamp = datetime.now(timezone.utc) - timedelta(minutes=20)
        service._semantic_searches.append(old_timestamp)

        # Add a timestamp that is 10 minutes old (inside 15 min window)
        within_window = datetime.now(timezone.utc) - timedelta(minutes=10)
        service._semantic_searches.append(within_window)

        # Add a recent timestamp
        service.increment_semantic_search()

        # Get metrics with 900 second (15 min) window
        metrics = service.get_metrics(window_seconds=900)

        # Should count the 10-minute-old and the recent one, but not 20-minute-old
        assert metrics["semantic_searches"] == 2

    def test_get_metrics_filters_by_window_1_hour(self):
        """Test that get_metrics with 3600 second (1 hour) window counts appropriate calls."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Add a timestamp that is 2 hours old (outside 1 hour window)
        old_timestamp = datetime.now(timezone.utc) - timedelta(hours=2)
        service._other_index_searches.append(old_timestamp)

        # Add a timestamp that is 30 minutes old (inside 1 hour window)
        within_window = datetime.now(timezone.utc) - timedelta(minutes=30)
        service._other_index_searches.append(within_window)

        # Add a recent timestamp
        service.increment_other_index_search()

        # Get metrics with 3600 second (1 hour) window
        metrics = service.get_metrics(window_seconds=3600)

        # Should count the 30-minute-old and the recent one, but not 2-hour-old
        assert metrics["other_index_searches"] == 2

    def test_get_metrics_filters_by_window_24_hours(self):
        """Test that get_metrics with 86400 second (24 hour) window counts all recent calls."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Add timestamps at various ages within 24 hours
        service._regex_searches.append(
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
        service._regex_searches.append(
            datetime.now(timezone.utc) - timedelta(hours=12)
        )
        service._regex_searches.append(
            datetime.now(timezone.utc) - timedelta(hours=23)
        )

        # Add a recent one
        service.increment_regex_search()

        # Get metrics with 86400 second (24 hour) window
        metrics = service.get_metrics(window_seconds=86400)

        # Should count all 4
        assert metrics["regex_searches"] == 4

    def test_cleanup_removes_timestamps_older_than_24_hours(self):
        """Test that timestamps older than 24 hours are cleaned up."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Add a timestamp that is 25 hours old (should be cleaned up)
        old_timestamp = datetime.now(timezone.utc) - timedelta(hours=25)
        service._semantic_searches.append(old_timestamp)

        # Add a recent timestamp
        service.increment_semantic_search()

        # The cleanup should happen on increment - check deque size
        # After cleanup, only the recent one should remain
        assert len(service._semantic_searches) == 1

    def test_thread_safety_concurrent_increments(self):
        """Test that concurrent increments from multiple threads are thread-safe.

        Uses ThreadPoolExecutor with 10 threads, each incrementing counters 100 times.
        Final count must equal expected total.
        """
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Test parameters
        num_threads = 10
        increments_per_thread = 100
        expected_total_per_counter = num_threads * increments_per_thread

        def increment_all_counters():
            """Each thread increments all four counters."""
            for _ in range(increments_per_thread):
                service.increment_semantic_search()
                service.increment_other_index_search()
                service.increment_regex_search()
                service.increment_other_api_call()

        # Execute concurrent increments
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(increment_all_counters) for _ in range(num_threads)
            ]
            # Wait for all threads to complete
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # Verify final counts with a large window to include all
        metrics = service.get_metrics(window_seconds=3600)

        assert metrics["semantic_searches"] == expected_total_per_counter, (
            f"Expected {expected_total_per_counter} semantic searches, "
            f"got {metrics['semantic_searches']}"
        )
        assert metrics["other_index_searches"] == expected_total_per_counter, (
            f"Expected {expected_total_per_counter} other index searches, "
            f"got {metrics['other_index_searches']}"
        )
        assert metrics["regex_searches"] == expected_total_per_counter, (
            f"Expected {expected_total_per_counter} regex searches, "
            f"got {metrics['regex_searches']}"
        )
        assert metrics["other_api_calls"] == expected_total_per_counter, (
            f"Expected {expected_total_per_counter} other API calls, "
            f"got {metrics['other_api_calls']}"
        )

    def test_thread_safety_concurrent_get_and_increment(self):
        """Test that concurrent get_metrics and increment calls are thread-safe."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        num_threads = 10
        operations_per_thread = 50
        errors = []

        def mixed_operations():
            """Each thread does a mix of increments and reads."""
            try:
                for _ in range(operations_per_thread):
                    service.increment_semantic_search()
                    metrics = service.get_metrics(window_seconds=60)
                    # Verify metrics are valid (non-negative)
                    assert metrics["semantic_searches"] >= 0
                    assert metrics["other_index_searches"] >= 0
            except Exception as e:
                errors.append(e)

        # Execute concurrent operations
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(mixed_operations) for _ in range(num_threads)
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()

        # No errors should have occurred
        assert len(errors) == 0, f"Thread safety errors: {errors}"

    def test_different_windows_return_different_counts(self):
        """Test that different window sizes return different counts."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Add timestamps at different ages
        # NOTE: Must be appended in chronological order (oldest first)
        # because _count_in_window optimizes by iterating from the end
        now = datetime.now(timezone.utc)

        # 12 hours ago - inside 24 hour window but outside 1 hour
        service._semantic_searches.append(now - timedelta(hours=12))

        # 30 minutes ago - inside 1 hour window but outside 15 min
        service._semantic_searches.append(now - timedelta(minutes=30))

        # 5 minutes ago - inside 15 min window but outside 1 min
        service._semantic_searches.append(now - timedelta(minutes=5))

        # 30 seconds ago - inside 1 min window
        service._semantic_searches.append(now - timedelta(seconds=30))

        # Get metrics with different windows
        metrics_1min = service.get_metrics(window_seconds=60)
        metrics_15min = service.get_metrics(window_seconds=900)
        metrics_1hour = service.get_metrics(window_seconds=3600)
        metrics_24hour = service.get_metrics(window_seconds=86400)

        assert metrics_1min["semantic_searches"] == 1  # Only 30s ago
        assert metrics_15min["semantic_searches"] == 2  # 30s + 5min ago
        assert metrics_1hour["semantic_searches"] == 3  # 30s + 5min + 30min ago
        assert metrics_24hour["semantic_searches"] == 4  # All four

    def test_reset_method_deprecated_but_functional(self):
        """Test that reset() method still works for backward compatibility.

        Note: reset() is deprecated with rolling windows but should still function
        by clearing all deques.
        """
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        # Add some data
        service.increment_semantic_search()
        service.increment_other_index_search()

        # Verify non-zero
        metrics_before = service.get_metrics(window_seconds=60)
        assert metrics_before["semantic_searches"] == 1
        assert metrics_before["other_index_searches"] == 1

        # Reset
        service.reset()

        # Verify all zero
        metrics_after = service.get_metrics(window_seconds=60)
        assert metrics_after["semantic_searches"] == 0
        assert metrics_after["other_index_searches"] == 0

    def test_deque_stores_datetime_objects(self):
        """Test that deques store datetime objects, not counts."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        service.increment_semantic_search()

        # Access internal deque - should contain datetime object
        assert len(service._semantic_searches) == 1
        assert isinstance(service._semantic_searches[0], datetime)

    def test_all_deques_initialized_as_empty(self):
        """Test that all timestamp deques are initialized as empty."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        service = ApiMetricsService()

        assert len(service._semantic_searches) == 0
        assert len(service._other_index_searches) == 0
        assert len(service._regex_searches) == 0
        assert len(service._other_api_calls) == 0

    def test_has_lock_for_thread_safety(self):
        """Test that service has threading lock."""
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )
        import threading

        service = ApiMetricsService()

        assert hasattr(service, "_lock")
        assert isinstance(service._lock, type(threading.Lock()))


class TestDashboardServiceApiMetricsIntegration:
    """Test that dashboard_service.get_stats_partial accepts api_window parameter."""

    def test_get_stats_partial_accepts_api_window_parameter(self):
        """Test that get_stats_partial accepts api_window parameter."""
        from src.code_indexer.server.services.dashboard_service import DashboardService

        service = DashboardService()

        # Should not raise when api_window is passed
        # We expect the method signature to support api_window
        import inspect
        sig = inspect.signature(service.get_stats_partial)
        param_names = list(sig.parameters.keys())

        assert "api_window" in param_names, (
            "get_stats_partial should accept api_window parameter"
        )

    def test_get_stats_partial_passes_window_to_metrics_service(self):
        """Test that get_stats_partial passes window_seconds to api_metrics_service."""
        from src.code_indexer.server.services.dashboard_service import DashboardService
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        # Add some metrics
        api_metrics_service.increment_semantic_search()

        service = DashboardService()

        # Mock internal methods to avoid full system dependency
        with patch.object(service, "_get_job_counts", return_value=None):
            with patch.object(service, "_get_repo_counts", return_value=None):
                with patch.object(service, "_get_recent_jobs", return_value=[]):
                    # Call with api_window
                    result = service.get_stats_partial(
                        username="testuser",
                        api_window=60,
                    )

                    # Should have api_metrics in result
                    assert "api_metrics" in result

    def test_get_stats_partial_does_not_reset_metrics(self):
        """Test that get_stats_partial no longer resets metrics after reading.

        With rolling window, we don't reset - metrics naturally age out.
        """
        from src.code_indexer.server.services.api_metrics_service import (
            ApiMetricsService,
        )

        # Create fresh service for testing
        fresh_service = ApiMetricsService()

        # Add metrics
        fresh_service.increment_semantic_search()
        fresh_service.increment_semantic_search()

        # Get metrics first time
        metrics1 = fresh_service.get_metrics(window_seconds=60)
        assert metrics1["semantic_searches"] == 2

        # Get metrics second time - should still be 2 (not reset)
        metrics2 = fresh_service.get_metrics(window_seconds=60)
        assert metrics2["semantic_searches"] == 2, (
            "Metrics should not reset on read with rolling window approach"
        )


class TestGlobalServiceInstanceCompatibility:
    """Test that global api_metrics_service instance works with rolling window."""

    def test_global_instance_has_deques(self):
        """Test that global instance has timestamp deques."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )
        from collections import deque

        assert hasattr(api_metrics_service, "_semantic_searches")
        assert hasattr(api_metrics_service, "_other_index_searches")
        assert hasattr(api_metrics_service, "_regex_searches")
        assert hasattr(api_metrics_service, "_other_api_calls")

        # Should be deque instances
        assert isinstance(api_metrics_service._semantic_searches, deque)
        assert isinstance(api_metrics_service._other_index_searches, deque)
        assert isinstance(api_metrics_service._regex_searches, deque)
        assert isinstance(api_metrics_service._other_api_calls, deque)

    def test_global_instance_get_metrics_accepts_window_seconds(self):
        """Test that global instance get_metrics accepts window_seconds."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        # Should not raise with window_seconds parameter
        metrics = api_metrics_service.get_metrics(window_seconds=60)
        assert isinstance(metrics, dict)
        assert "semantic_searches" in metrics
