"""
Tests for Dashboard Memory Metrics - RSS, Index Memory, and Swap (Story #358).

Tests verify:
- SystemHealthInfo model accepts new fields (process_rss_mb, index_memory_mb,
  swap_used_mb, swap_total_mb)
- system_metrics_collector collects process RSS, index memory (via callback), swap metrics
- index_memory_mb defaults to 0.0 when no provider is registered
- health_service._get_system_info() returns new fields
"""

from __future__ import annotations

import unittest

import pytest

from code_indexer.server.models.api_models import SystemHealthInfo
from code_indexer.server.services.system_metrics_collector import (
    SystemMetricsCollector,
    reset_system_metrics_collector,
)


def _make_system_health_info(**kwargs: object) -> SystemHealthInfo:
    """Helper to create SystemHealthInfo with required fields pre-filled."""
    defaults = {
        "memory_usage_percent": 50.0,
        "cpu_usage_percent": 25.0,
        "active_jobs": 0,
        "disk_free_space_gb": 100.0,
    }
    defaults.update(kwargs)  # type: ignore[arg-type]
    return SystemHealthInfo(**defaults)  # type: ignore[arg-type]


class TestSystemHealthInfoMemoryFields(unittest.TestCase):
    """Test that SystemHealthInfo model accepts and stores new memory fields."""

    def test_system_health_info_has_process_rss_mb_field(self) -> None:
        """SystemHealthInfo must have process_rss_mb field with default 0.0."""
        info = _make_system_health_info()
        self.assertEqual(info.process_rss_mb, 0.0)

    def test_system_health_info_has_index_memory_mb_field(self) -> None:
        """SystemHealthInfo must have index_memory_mb field with default 0.0."""
        info = _make_system_health_info()
        self.assertEqual(info.index_memory_mb, 0.0)

    def test_system_health_info_has_swap_used_mb_field(self) -> None:
        """SystemHealthInfo must have swap_used_mb field with default 0.0."""
        info = _make_system_health_info()
        self.assertEqual(info.swap_used_mb, 0.0)

    def test_system_health_info_has_swap_total_mb_field(self) -> None:
        """SystemHealthInfo must have swap_total_mb field with default 0.0."""
        info = _make_system_health_info()
        self.assertEqual(info.swap_total_mb, 0.0)

    def test_system_health_info_accepts_nonzero_memory_fields(self) -> None:
        """SystemHealthInfo must accept non-zero values for all new memory fields."""
        info = _make_system_health_info(
            process_rss_mb=512.5,
            index_memory_mb=128.3,
            swap_used_mb=1024.0,
            swap_total_mb=2048.0,
        )
        self.assertAlmostEqual(info.process_rss_mb, 512.5)
        self.assertAlmostEqual(info.index_memory_mb, 128.3)
        self.assertAlmostEqual(info.swap_used_mb, 1024.0)
        self.assertAlmostEqual(info.swap_total_mb, 2048.0)

    def test_system_health_info_memory_fields_are_floats(self) -> None:
        """All new memory fields must be float type."""
        info = _make_system_health_info(
            process_rss_mb=256,
            index_memory_mb=64,
            swap_used_mb=512,
            swap_total_mb=1024,
        )
        self.assertIsInstance(info.process_rss_mb, float)
        self.assertIsInstance(info.index_memory_mb, float)
        self.assertIsInstance(info.swap_used_mb, float)
        self.assertIsInstance(info.swap_total_mb, float)


class TestSystemMetricsCollectorMemoryMetrics(unittest.TestCase):
    """Test that SystemMetricsCollector collects the new memory metrics."""

    def setUp(self) -> None:
        reset_system_metrics_collector()

    def tearDown(self) -> None:
        reset_system_metrics_collector()

    def test_collector_has_get_process_rss_method(self) -> None:
        """SystemMetricsCollector must have get_process_rss() method."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        self.assertTrue(hasattr(collector, "get_process_rss"))
        self.assertTrue(callable(collector.get_process_rss))

    def test_collector_has_get_index_memory_method(self) -> None:
        """SystemMetricsCollector must have get_index_memory() method."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        self.assertTrue(hasattr(collector, "get_index_memory"))
        self.assertTrue(callable(collector.get_index_memory))

    def test_collector_has_get_swap_usage_method(self) -> None:
        """SystemMetricsCollector must have get_swap_usage() method."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        self.assertTrue(hasattr(collector, "get_swap_usage"))
        self.assertTrue(callable(collector.get_swap_usage))

    def test_get_process_rss_returns_positive_float(self) -> None:
        """get_process_rss() must return a positive float (real RSS from this process)."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        # Allow background thread to populate cache
        import time

        time.sleep(0.2)
        rss = collector.get_process_rss()
        self.assertIsInstance(rss, float)
        self.assertGreater(rss, 0.0, "Process RSS must be positive")

    def test_get_swap_usage_returns_dict_with_used_and_total(self) -> None:
        """get_swap_usage() must return dict with 'used_mb' and 'total_mb' keys."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        import time

        time.sleep(0.2)
        swap = collector.get_swap_usage()
        self.assertIsInstance(swap, dict)
        self.assertIn("used_mb", swap)
        self.assertIn("total_mb", swap)
        self.assertIsInstance(swap["used_mb"], float)
        self.assertIsInstance(swap["total_mb"], float)
        self.assertGreaterEqual(swap["used_mb"], 0.0)
        self.assertGreaterEqual(swap["total_mb"], 0.0)

    def test_get_index_memory_returns_non_negative_float(self) -> None:
        """get_index_memory() must return a non-negative float."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        import time

        time.sleep(0.2)
        index_memory_mb = collector.get_index_memory()
        self.assertIsInstance(index_memory_mb, float)
        self.assertGreaterEqual(index_memory_mb, 0.0)

    def test_index_memory_defaults_to_zero_when_no_provider(self) -> None:
        """get_index_memory() must return 0.0 when no provider is registered."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        # Manually set cache with index_memory_mb=0.0 (no provider registered)
        with collector._cache_lock:
            collector._cached_metrics = {
                "cpu_usage": 10.0,
                "memory": {"percent": 50.0, "used_bytes": 1000000},
                "disk": {"free_bytes": 1000000, "read_bytes": 0, "write_bytes": 0},
                "network": {"receive_bytes": 0, "transmit_bytes": 0},
                "process_rss_mb": 256.0,
                "index_memory_mb": 0.0,  # No provider registered
                "swap_used_mb": 512.0,
                "swap_total_mb": 1024.0,
            }
        index_mb = collector.get_index_memory()
        self.assertEqual(index_mb, 0.0)

    def test_cached_metrics_include_new_memory_keys(self) -> None:
        """After refresh, cached metrics must contain the new memory metric keys."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        import time

        time.sleep(0.3)
        with collector._cache_lock:
            metrics = collector._cached_metrics
        self.assertIsNotNone(metrics)
        assert metrics is not None
        self.assertIn("process_rss_mb", metrics)
        self.assertIn("index_memory_mb", metrics)
        self.assertIn("swap_used_mb", metrics)
        self.assertIn("swap_total_mb", metrics)


@pytest.mark.slow
class TestHealthServiceSystemInfoMemoryFields(unittest.TestCase):
    """Test that health_service._get_system_info() returns new memory fields."""

    def test_get_system_info_returns_process_rss_mb(self) -> None:
        """_get_system_info() must return SystemHealthInfo with process_rss_mb > 0."""
        from code_indexer.server.services.health_service import HealthCheckService

        # Minimal HealthCheckService construction - just enough for _get_system_info
        service = HealthCheckService.__new__(HealthCheckService)
        service._last_disk_counters = None
        service._last_disk_time = None
        service._last_net_counters = None
        service._last_net_time = None
        service._cpu_history = []
        service._cpu_history_lock = __import__("threading").Lock()

        result = service._get_system_info()
        self.assertIsInstance(result, SystemHealthInfo)
        self.assertGreater(
            result.process_rss_mb,
            0.0,
            "process_rss_mb must be > 0 for a running process",
        )

    def test_get_system_info_returns_swap_fields(self) -> None:
        """_get_system_info() must return SystemHealthInfo with swap_total_mb >= 0."""
        from code_indexer.server.services.health_service import HealthCheckService

        service = HealthCheckService.__new__(HealthCheckService)
        service._last_disk_counters = None
        service._last_disk_time = None
        service._last_net_counters = None
        service._last_net_time = None
        service._cpu_history = []
        service._cpu_history_lock = __import__("threading").Lock()

        result = service._get_system_info()
        self.assertGreaterEqual(result.swap_used_mb, 0.0)
        self.assertGreaterEqual(result.swap_total_mb, 0.0)

    def test_get_system_info_returns_index_memory_mb(self) -> None:
        """_get_system_info() must return SystemHealthInfo with index_memory_mb >= 0."""
        from code_indexer.server.services.health_service import HealthCheckService

        service = HealthCheckService.__new__(HealthCheckService)
        service._last_disk_counters = None
        service._last_disk_time = None
        service._last_net_counters = None
        service._last_net_time = None
        service._cpu_history = []
        service._cpu_history_lock = __import__("threading").Lock()

        result = service._get_system_info()
        self.assertGreaterEqual(result.index_memory_mb, 0.0)

    def test_get_system_info_index_memory_mb_is_zero_when_caches_empty(self) -> None:
        """_get_system_info() must return 0.0 for index_memory_mb when caches not loaded."""
        from code_indexer.server.services.health_service import HealthCheckService
        from code_indexer.server.cache import reset_global_cache, reset_global_fts_cache

        # Ensure caches are empty so index_memory_mb reports 0.0
        reset_global_cache()
        reset_global_fts_cache()

        service = HealthCheckService.__new__(HealthCheckService)
        service._last_disk_counters = None
        service._last_disk_time = None
        service._last_net_counters = None
        service._last_net_time = None
        service._cpu_history = []
        service._cpu_history_lock = __import__("threading").Lock()

        result = service._get_system_info()
        self.assertEqual(
            result.index_memory_mb,
            0.0,
            "index_memory_mb must be 0.0 when no indexes are cached",
        )


if __name__ == "__main__":
    unittest.main()
