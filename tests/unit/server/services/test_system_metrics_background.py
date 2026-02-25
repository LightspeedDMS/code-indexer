"""
Tests for Story #278: SystemMetricsCollector uses background refresh thread.

Currently every get_* method acquires _cache_lock and, if cache is stale,
calls _refresh_cache() which makes multiple blocking psutil system calls under
the lock. This serializes all concurrent health check requests.

Fix: Move psutil calls to a background refresh thread that runs on a timer
(every cache_ttl seconds). The get_* methods then only read cached values
under the lock (fast dict reads, no blocking I/O).

Key requirements tested:
- Background refresh thread starts on construction
- get_all_metrics returns cached values without calling psutil directly
- Cache is populated by the background thread within TTL period
- reset_system_metrics_collector stops the background thread
- get_* methods still return valid metric data
"""

import threading
import time
from unittest.mock import patch, MagicMock

from code_indexer.server.services.system_metrics_collector import (
    SystemMetricsCollector,
    get_system_metrics_collector,
    reset_system_metrics_collector,
)


class TestBackgroundRefreshThreadStarts:
    """Verify background refresh thread is started on construction."""

    def setup_method(self):
        """Reset singleton before each test."""
        reset_system_metrics_collector()

    def teardown_method(self):
        """Reset singleton after each test."""
        reset_system_metrics_collector()

    def test_background_thread_attribute_exists(self):
        """SystemMetricsCollector must have a background refresh thread attribute."""
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)
        assert hasattr(collector, "_refresh_thread"), (
            "SystemMetricsCollector must have a _refresh_thread attribute"
        )

    def test_background_thread_is_alive_after_construction(self):
        """Background refresh thread must be running after construction."""
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)
        assert collector._refresh_thread is not None, (
            "_refresh_thread must not be None after construction"
        )
        assert collector._refresh_thread.is_alive(), (
            "Background refresh thread must be alive after construction"
        )

    def test_background_thread_is_daemon(self):
        """Background refresh thread must be a daemon thread (won't block shutdown)."""
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)
        assert collector._refresh_thread.daemon, (
            "Background refresh thread must be a daemon thread"
        )


class TestGetMethodsReturnCachedValues:
    """Verify get_* methods return cached values without calling psutil directly."""

    def setup_method(self):
        reset_system_metrics_collector()

    def teardown_method(self):
        reset_system_metrics_collector()

    def test_get_all_metrics_does_not_call_psutil_directly(self):
        """
        get_all_metrics must NOT call psutil functions directly.
        psutil calls only happen in the background thread.
        """
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)

        # Wait briefly for background thread to populate cache
        time.sleep(0.2)

        # Now verify get_all_metrics does NOT call psutil during the call itself
        psutil_calls = []

        with patch("psutil.cpu_percent", side_effect=lambda **kw: psutil_calls.append("cpu") or 10.0):
            with patch("psutil.virtual_memory", side_effect=lambda: psutil_calls.append("mem") or MagicMock(percent=50.0, used=1000)):
                metrics = collector.get_all_metrics()

        assert len(psutil_calls) == 0, (
            "get_all_metrics must NOT call psutil directly - "
            "psutil calls must only happen in the background refresh thread. "
            f"Unexpected psutil calls: {psutil_calls}"
        )
        assert metrics is not None

    def test_get_cpu_usage_does_not_call_psutil_directly(self):
        """get_cpu_usage must return cached value without direct psutil call."""
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)
        time.sleep(0.2)

        psutil_calls = []
        with patch("psutil.cpu_percent", side_effect=lambda **kw: psutil_calls.append("cpu") or 10.0):
            cpu = collector.get_cpu_usage()

        assert len(psutil_calls) == 0, (
            "get_cpu_usage must NOT call psutil.cpu_percent directly"
        )
        assert isinstance(cpu, float)

    def test_get_memory_usage_does_not_call_psutil_directly(self):
        """get_memory_usage must return cached value without direct psutil call."""
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)
        time.sleep(0.2)

        psutil_calls = []
        with patch("psutil.virtual_memory", side_effect=lambda: psutil_calls.append("mem") or MagicMock(percent=50.0, used=1000)):
            mem = collector.get_memory_usage()

        assert len(psutil_calls) == 0, (
            "get_memory_usage must NOT call psutil.virtual_memory directly"
        )
        assert "percent" in mem
        assert "used_bytes" in mem


class TestCachePopulatedByBackgroundThread:
    """Verify cache is populated by background thread within TTL period."""

    def setup_method(self):
        reset_system_metrics_collector()

    def teardown_method(self):
        reset_system_metrics_collector()

    def test_cache_is_populated_after_short_wait(self):
        """Background thread must populate cache within the TTL period."""
        collector = SystemMetricsCollector(cache_ttl_seconds=0.5)

        # Wait for background thread to run at least once
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if collector._cached_metrics is not None:
                break
            time.sleep(0.05)

        assert collector._cached_metrics is not None, (
            "Background thread must populate _cached_metrics within 2 seconds"
        )

    def test_metrics_values_are_valid_numbers(self):
        """Background-populated cache must contain valid numeric metrics."""
        collector = SystemMetricsCollector(cache_ttl_seconds=0.5)

        # Wait for background thread
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if collector._cached_metrics is not None:
                break
            time.sleep(0.05)

        assert collector._cached_metrics is not None

        metrics = collector.get_all_metrics()
        assert isinstance(metrics["cpu_usage"], float)
        assert 0.0 <= metrics["cpu_usage"] <= 100.0
        assert isinstance(metrics["memory"]["percent"], float)
        assert 0.0 <= metrics["memory"]["percent"] <= 100.0
        assert isinstance(metrics["memory"]["used_bytes"], int)


class TestBackgroundThreadStopsOnReset:
    """Verify reset_system_metrics_collector stops the background thread."""

    def setup_method(self):
        reset_system_metrics_collector()

    def teardown_method(self):
        reset_system_metrics_collector()

    def test_collector_has_stop_method_or_stop_event(self):
        """SystemMetricsCollector must have a way to stop the background thread."""
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)
        has_stop = hasattr(collector, "stop") or hasattr(collector, "_stop_event")
        assert has_stop, (
            "SystemMetricsCollector must have a stop() method or _stop_event "
            "to allow clean shutdown of the background thread"
        )

    def test_reset_stops_background_thread(self):
        """After reset_system_metrics_collector, the thread must be stoppable."""
        collector = SystemMetricsCollector(cache_ttl_seconds=5.0)
        assert collector._refresh_thread.is_alive()

        # Stop the collector
        if hasattr(collector, "stop"):
            collector.stop()
        elif hasattr(collector, "_stop_event"):
            collector._stop_event.set()

        # Give thread time to stop
        collector._refresh_thread.join(timeout=2.0)
        assert not collector._refresh_thread.is_alive(), (
            "Background thread must stop after stop signal is sent"
        )


class TestGetSystemMetricsCollectorSingleton:
    """Verify the singleton factory still works correctly."""

    def setup_method(self):
        reset_system_metrics_collector()

    def teardown_method(self):
        reset_system_metrics_collector()

    def test_returns_same_instance(self):
        """get_system_metrics_collector returns the same singleton."""
        c1 = get_system_metrics_collector()
        c2 = get_system_metrics_collector()
        assert c1 is c2

    def test_singleton_has_running_background_thread(self):
        """Singleton instance must have a running background thread."""
        collector = get_system_metrics_collector()
        assert collector._refresh_thread.is_alive()
