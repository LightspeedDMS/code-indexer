"""
Unit tests for Story #4 AC2: Aggregated API Call Metrics.

Tests for API metrics service that tracks call counts by category:
- Semantic Searches (search_code with semantic mode)
- Other Index Searches (FTS, temporal, hybrid searches)
- Regex Searches (regex_search calls)
- All Other API Calls (remaining API endpoints)

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import concurrent.futures


class TestApiMetricsService:
    """Test AC2: API call metrics tracking."""

    def test_increment_semantic_search_counter(self):
        """Test that semantic search calls are tracked."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        # Reset to known state
        api_metrics_service.reset()

        # Increment semantic search counter
        api_metrics_service.increment_semantic_search()

        # Get metrics
        metrics = api_metrics_service.get_metrics()

        assert metrics["semantic_searches"] == 1
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0

    def test_increment_other_index_search_counter(self):
        """Test that FTS/temporal/hybrid searches are tracked as other_index_searches."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        api_metrics_service.reset()

        # Increment other index search counter (FTS, temporal, hybrid)
        api_metrics_service.increment_other_index_search()
        api_metrics_service.increment_other_index_search()

        metrics = api_metrics_service.get_metrics()

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 2
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0

    def test_increment_regex_search_counter(self):
        """Test that regex search calls are tracked."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        api_metrics_service.reset()

        api_metrics_service.increment_regex_search()

        metrics = api_metrics_service.get_metrics()

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 1
        assert metrics["other_api_calls"] == 0

    def test_increment_other_api_calls_counter(self):
        """Test that other API calls are tracked."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        api_metrics_service.reset()

        api_metrics_service.increment_other_api_call()
        api_metrics_service.increment_other_api_call()
        api_metrics_service.increment_other_api_call()

        metrics = api_metrics_service.get_metrics()

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 3

    def test_thread_safety_concurrent_increments(self):
        """Test that concurrent increments from multiple threads are thread-safe.

        Code Review Finding #5: Verify thread-safe concurrent access to counters.
        Uses ThreadPoolExecutor with 10 threads, each incrementing counters 100 times.
        Final count must equal expected total (10 threads * 100 increments = 1000 per counter).
        """
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        # Reset to known state
        api_metrics_service.reset()

        # Test parameters
        num_threads = 10
        increments_per_thread = 100
        expected_total_per_counter = num_threads * increments_per_thread

        def increment_all_counters():
            """Each thread increments all four counters."""
            for _ in range(increments_per_thread):
                api_metrics_service.increment_semantic_search()
                api_metrics_service.increment_other_index_search()
                api_metrics_service.increment_regex_search()
                api_metrics_service.increment_other_api_call()

        # Execute concurrent increments
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [
                executor.submit(increment_all_counters) for _ in range(num_threads)
            ]
            # Wait for all threads to complete
            for future in concurrent.futures.as_completed(futures):
                future.result()  # Raises any exception from threads

        # Verify final counts
        metrics = api_metrics_service.get_metrics()

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

    def test_reset_clears_all_counters(self):
        """Test that reset() clears all counters to zero."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        # Set up some non-zero values
        api_metrics_service.reset()
        api_metrics_service.increment_semantic_search()
        api_metrics_service.increment_other_index_search()
        api_metrics_service.increment_regex_search()
        api_metrics_service.increment_other_api_call()

        # Verify non-zero
        metrics_before = api_metrics_service.get_metrics()
        assert metrics_before["semantic_searches"] == 1
        assert metrics_before["other_index_searches"] == 1
        assert metrics_before["regex_searches"] == 1
        assert metrics_before["other_api_calls"] == 1

        # Reset
        api_metrics_service.reset()

        # Verify all zero
        metrics_after = api_metrics_service.get_metrics()
        assert metrics_after["semantic_searches"] == 0
        assert metrics_after["other_index_searches"] == 0
        assert metrics_after["regex_searches"] == 0
        assert metrics_after["other_api_calls"] == 0
