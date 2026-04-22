"""
Tests for real index memory footprint tracking (Story #526: replace mmap metric).

Tests verify:
- HNSWIndexCacheEntry stores real index_size_bytes from index_file_size()
- FTSIndexCacheEntry stores real index_size_bytes from directory scan
- get_stats() returns real total_memory_mb (not hardcoded ESTIMATED_INDEX_SIZE_MB)
- get_total_index_memory_mb() in cache/__init__.py combines both caches
- SystemMetricsCollector.set_index_memory_provider() callback mechanism
- SystemMetricsCollector uses index_memory_mb key (not mmap_total_mb)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
    HNSWIndexCacheEntry,
)
from code_indexer.server.cache.fts_index_cache import (
    FTSIndexCache,
    FTSIndexCacheConfig,
    FTSIndexCacheEntry,
)
from code_indexer.server.services.system_metrics_collector import (
    SystemMetricsCollector,
    reset_system_metrics_collector,
)


class TestHNSWIndexCacheEntryHasIndexSizeBytes(unittest.TestCase):
    """HNSWIndexCacheEntry must have index_size_bytes field."""

    def test_entry_has_index_size_bytes_field_defaulting_to_zero(self) -> None:
        """HNSWIndexCacheEntry must have index_size_bytes field with default 0."""
        mock_index = MagicMock()
        entry = HNSWIndexCacheEntry(
            hnsw_index=mock_index,
            id_mapping={},
            repo_path="/some/repo",
            ttl_minutes=10.0,
        )
        self.assertEqual(entry.index_size_bytes, 0)

    def test_entry_accepts_nonzero_index_size_bytes(self) -> None:
        """HNSWIndexCacheEntry must accept non-zero index_size_bytes."""
        mock_index = MagicMock()
        entry = HNSWIndexCacheEntry(
            hnsw_index=mock_index,
            id_mapping={},
            repo_path="/some/repo",
            ttl_minutes=10.0,
            index_size_bytes=52428800,  # 50 MB
        )
        self.assertEqual(entry.index_size_bytes, 52428800)


class TestHNSWIndexCacheGetOrLoadCapturesRealSize(unittest.TestCase):
    """HNSWIndexCache.get_or_load() must capture real size via index_file_size()."""

    def setUp(self) -> None:
        config = HNSWIndexCacheConfig(ttl_minutes=10.0, cleanup_interval_seconds=60)
        self.cache = HNSWIndexCache(config=config)

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_get_or_load_stores_index_size_bytes_from_index_file_size(self) -> None:
        """After get_or_load, index_size_bytes must equal index_file_size() + sys.getsizeof(id_mapping).

        Bug #881 Phase 4: id_mapping dict overhead is now included so the cache
        size cap has an accurate view of total Python memory usage.
        """
        file_size_bytes = 104857600  # 100 MB
        empty_id_mapping: Dict = {}
        expected_bytes = file_size_bytes + sys.getsizeof(empty_id_mapping)

        mock_index = MagicMock()
        mock_index.index_file_size.return_value = file_size_bytes

        def loader() -> Tuple[Any, Dict]:
            return mock_index, empty_id_mapping

        with tempfile.TemporaryDirectory() as tmpdir:
            self.cache.get_or_load(tmpdir, loader)

            repo_path = str(Path(tmpdir).resolve())
            with self.cache._cache_lock:
                entry = self.cache._cache.get(repo_path)

            self.assertIsNotNone(entry, "Entry should be in cache after get_or_load")
            assert entry is not None
            self.assertEqual(entry.index_size_bytes, expected_bytes)

    def test_get_or_load_stores_id_mapping_size_when_index_file_size_raises_attribute_error(
        self,
    ) -> None:
        """When index_file_size() raises AttributeError, index_size_bytes must equal sys.getsizeof(id_mapping)."""
        empty_id_mapping: Dict = {}
        mock_index = MagicMock()
        mock_index.index_file_size.side_effect = AttributeError("no such method")

        def loader() -> Tuple[Any, Dict]:
            return mock_index, empty_id_mapping

        with tempfile.TemporaryDirectory() as tmpdir:
            self.cache.get_or_load(tmpdir, loader)

            repo_path = str(Path(tmpdir).resolve())
            with self.cache._cache_lock:
                entry = self.cache._cache.get(repo_path)

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.index_size_bytes, sys.getsizeof(empty_id_mapping))

    def test_get_or_load_stores_id_mapping_size_when_index_file_size_raises_exception(
        self,
    ) -> None:
        """When index_file_size() raises any Exception, index_size_bytes must equal sys.getsizeof(id_mapping)."""
        empty_id_mapping: Dict = {}
        mock_index = MagicMock()
        mock_index.index_file_size.side_effect = RuntimeError("unexpected error")

        def loader() -> Tuple[Any, Dict]:
            return mock_index, empty_id_mapping

        with tempfile.TemporaryDirectory() as tmpdir:
            self.cache.get_or_load(tmpdir, loader)

            repo_path = str(Path(tmpdir).resolve())
            with self.cache._cache_lock:
                entry = self.cache._cache.get(repo_path)

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.index_size_bytes, sys.getsizeof(empty_id_mapping))


class TestHNSWIndexCacheGetStatsUsesRealSize(unittest.TestCase):
    """HNSWIndexCache.get_stats() must use real index_size_bytes, not ESTIMATED_INDEX_SIZE_MB."""

    def setUp(self) -> None:
        config = HNSWIndexCacheConfig(ttl_minutes=10.0, cleanup_interval_seconds=60)
        self.cache = HNSWIndexCache(config=config)

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_get_stats_total_memory_mb_uses_real_size(self) -> None:
        """get_stats().total_memory_mb must equal sum of index_size_bytes / (1024*1024)."""
        expected_bytes = 52428800  # 50 MB

        mock_index = MagicMock()
        mock_index.index_file_size.return_value = expected_bytes

        def loader() -> Tuple[Any, Dict]:
            return mock_index, {}

        with tempfile.TemporaryDirectory() as tmpdir:
            self.cache.get_or_load(tmpdir, loader)
            stats = self.cache.get_stats()

        expected_mb = expected_bytes / (1024 * 1024)
        self.assertAlmostEqual(stats.total_memory_mb, expected_mb, places=2)

    def test_get_stats_total_memory_mb_not_hardcoded_estimate(self) -> None:
        """get_stats().total_memory_mb must NOT be len(cache)*ESTIMATED_INDEX_SIZE_MB."""
        # ESTIMATED_INDEX_SIZE_MB = 100, so 1 entry should NOT give 100.0 MB
        # unless index_file_size() returns exactly 100*1024*1024 bytes
        small_bytes = 1024 * 1024  # 1 MB (much less than ESTIMATED_INDEX_SIZE_MB=100)

        mock_index = MagicMock()
        mock_index.index_file_size.return_value = small_bytes

        def loader() -> Tuple[Any, Dict]:
            return mock_index, {}

        with tempfile.TemporaryDirectory() as tmpdir:
            self.cache.get_or_load(tmpdir, loader)
            stats = self.cache.get_stats()

        # Must be ~1.0 MB, NOT 100.0 MB (the hardcoded estimate)
        self.assertAlmostEqual(stats.total_memory_mb, 1.0, places=2)
        self.assertNotAlmostEqual(
            stats.total_memory_mb,
            100,
            places=0,
            msg="total_memory_mb must not equal 100 MB (hardcoded estimate)",
        )

    def test_get_stats_multiple_entries_sum_real_sizes(self) -> None:
        """get_stats().total_memory_mb must be sum of all entry index_size_bytes."""
        bytes_1 = 10 * 1024 * 1024  # 10 MB
        bytes_2 = 20 * 1024 * 1024  # 20 MB

        mock_index_1 = MagicMock()
        mock_index_1.index_file_size.return_value = bytes_1

        mock_index_2 = MagicMock()
        mock_index_2.index_file_size.return_value = bytes_2

        with tempfile.TemporaryDirectory() as tmpdir1:
            with tempfile.TemporaryDirectory() as tmpdir2:

                def loader1() -> Tuple[Any, Dict]:
                    return mock_index_1, {}

                def loader2() -> Tuple[Any, Dict]:
                    return mock_index_2, {}

                self.cache.get_or_load(tmpdir1, loader1)
                self.cache.get_or_load(tmpdir2, loader2)
                stats = self.cache.get_stats()

        expected_mb = (bytes_1 + bytes_2) / (1024 * 1024)
        self.assertAlmostEqual(stats.total_memory_mb, expected_mb, places=2)


class TestHNSWIndexCacheEnforceSizeLimitUsesRealSize(unittest.TestCase):
    """HNSWIndexCache._enforce_size_limit() must use real index_size_bytes."""

    def test_enforce_size_limit_uses_real_sizes_for_eviction(self) -> None:
        """_enforce_size_limit() must evict based on real index_size_bytes, not estimates."""
        # Limit 15 MB, two entries of 10 MB each - second should trigger eviction
        config = HNSWIndexCacheConfig(
            ttl_minutes=10.0,
            cleanup_interval_seconds=60,
            max_cache_size_mb=15,
        )
        cache = HNSWIndexCache(config=config)

        try:
            bytes_10mb = 10 * 1024 * 1024

            mock_index_1 = MagicMock()
            mock_index_1.index_file_size.return_value = bytes_10mb

            mock_index_2 = MagicMock()
            mock_index_2.index_file_size.return_value = bytes_10mb

            with tempfile.TemporaryDirectory() as tmpdir1:
                with tempfile.TemporaryDirectory() as tmpdir2:

                    def loader1() -> Tuple[Any, Dict]:
                        return mock_index_1, {}

                    def loader2() -> Tuple[Any, Dict]:
                        return mock_index_2, {}

                    cache.get_or_load(tmpdir1, loader1)
                    cache.get_or_load(tmpdir2, loader2)

                    # Cache must have evicted one entry to stay within 15 MB
                    with cache._cache_lock:
                        cache_size = len(cache._cache)

                    self.assertLessEqual(
                        cache_size,
                        1,
                        "Cache should evict entries when real size exceeds limit",
                    )
        finally:
            cache.stop_background_cleanup()


class TestFTSIndexCacheEntryHasIndexSizeBytes(unittest.TestCase):
    """FTSIndexCacheEntry must have index_size_bytes field."""

    def test_entry_has_index_size_bytes_field_defaulting_to_zero(self) -> None:
        """FTSIndexCacheEntry must have index_size_bytes field with default 0."""
        mock_index = MagicMock()
        mock_schema = MagicMock()
        entry = FTSIndexCacheEntry(
            tantivy_index=mock_index,
            schema=mock_schema,
            index_dir="/some/dir",
            ttl_minutes=10.0,
        )
        self.assertEqual(entry.index_size_bytes, 0)

    def test_entry_accepts_nonzero_index_size_bytes(self) -> None:
        """FTSIndexCacheEntry must accept non-zero index_size_bytes."""
        mock_index = MagicMock()
        mock_schema = MagicMock()
        entry = FTSIndexCacheEntry(
            tantivy_index=mock_index,
            schema=mock_schema,
            index_dir="/some/dir",
            ttl_minutes=10.0,
            index_size_bytes=10485760,  # 10 MB
        )
        self.assertEqual(entry.index_size_bytes, 10485760)


class TestFTSIndexCacheGetOrLoadCapturesRealDirSize(unittest.TestCase):
    """FTSIndexCache.get_or_load() must compute real directory size for index_size_bytes."""

    def setUp(self) -> None:
        config = FTSIndexCacheConfig(
            ttl_minutes=10.0,
            cleanup_interval_seconds=60,
            reload_on_access=False,
        )
        self.cache = FTSIndexCache(config=config)

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_get_or_load_stores_real_directory_size(self) -> None:
        """After get_or_load, entry index_size_bytes must equal actual files in dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some fake FTS index files
            file1 = Path(tmpdir) / "meta.json"
            file1.write_bytes(b"x" * 1024)  # 1 KB

            file2 = Path(tmpdir) / "segment_0.idx"
            file2.write_bytes(b"y" * 2048)  # 2 KB

            total_expected = 1024 + 2048  # 3 KB

            mock_index = MagicMock()
            mock_index.reload = MagicMock()
            mock_schema = MagicMock()

            def loader() -> Tuple[Any, Any]:
                return mock_index, mock_schema

            self.cache.get_or_load(tmpdir, loader)

            index_dir = str(Path(tmpdir).resolve())
            with self.cache._cache_lock:
                entry = self.cache._cache.get(index_dir)

            self.assertIsNotNone(entry, "Entry should be in cache after get_or_load")
            assert entry is not None
            self.assertEqual(entry.index_size_bytes, total_expected)

    def test_get_or_load_stores_zero_when_dir_does_not_exist(self) -> None:
        """When index directory does not exist, index_size_bytes must be 0."""
        mock_index = MagicMock()
        mock_index.reload = MagicMock()
        mock_schema = MagicMock()

        nonexistent_dir = "/tmp/nonexistent_fts_index_dir_12345_testing"

        def loader() -> Tuple[Any, Any]:
            return mock_index, mock_schema

        self.cache.get_or_load(nonexistent_dir, loader)

        index_dir = str(Path(nonexistent_dir).resolve())
        with self.cache._cache_lock:
            entry = self.cache._cache.get(index_dir)

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.index_size_bytes, 0)

    def test_get_or_load_includes_nested_files(self) -> None:
        """Directory scan for index_size_bytes must include subdirectory files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create nested directory structure (Tantivy creates segments)
            subdir = Path(tmpdir) / "segments"
            subdir.mkdir()

            file1 = Path(tmpdir) / "meta.json"
            file1.write_bytes(b"x" * 500)

            file2 = subdir / "segment_0.store"
            file2.write_bytes(b"y" * 1500)

            total_expected = 500 + 1500  # 2000 bytes

            mock_index = MagicMock()
            mock_index.reload = MagicMock()
            mock_schema = MagicMock()

            def loader() -> Tuple[Any, Any]:
                return mock_index, mock_schema

            self.cache.get_or_load(tmpdir, loader)

            index_dir = str(Path(tmpdir).resolve())
            with self.cache._cache_lock:
                entry = self.cache._cache.get(index_dir)

            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry.index_size_bytes, total_expected)


class TestFTSIndexCacheGetStatsUsesRealSize(unittest.TestCase):
    """FTSIndexCache.get_stats() must use real index_size_bytes, not ESTIMATED_INDEX_SIZE_MB."""

    def setUp(self) -> None:
        config = FTSIndexCacheConfig(
            ttl_minutes=10.0,
            cleanup_interval_seconds=60,
            reload_on_access=False,
        )
        self.cache = FTSIndexCache(config=config)

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_get_stats_total_memory_mb_uses_real_dir_size(self) -> None:
        """get_stats().total_memory_mb must equal real directory size / (1024*1024)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "data.bin"
            file1.write_bytes(b"z" * (5 * 1024 * 1024))  # 5 MB

            mock_index = MagicMock()
            mock_index.reload = MagicMock()
            mock_schema = MagicMock()

            def loader() -> Tuple[Any, Any]:
                return mock_index, mock_schema

            self.cache.get_or_load(tmpdir, loader)
            stats = self.cache.get_stats()

        expected_mb = (5 * 1024 * 1024) / (1024 * 1024)
        self.assertAlmostEqual(stats.total_memory_mb, expected_mb, places=2)

    def test_get_stats_total_memory_not_hardcoded_estimate(self) -> None:
        """get_stats().total_memory_mb must NOT be len(cache)*ESTIMATED_INDEX_SIZE_MB."""
        # ESTIMATED_INDEX_SIZE_MB = 10, so 1 entry should NOT give 10.0 MB
        # unless directory contains exactly 10 MB of files
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "tiny.dat"
            file1.write_bytes(b"a" * 1024)  # 1 KB (much less than ESTIMATED=10MB)

            mock_index = MagicMock()
            mock_index.reload = MagicMock()
            mock_schema = MagicMock()

            def loader() -> Tuple[Any, Any]:
                return mock_index, mock_schema

            self.cache.get_or_load(tmpdir, loader)
            stats = self.cache.get_stats()

        # Must be ~0.001 MB (1 KB), NOT 10 MB (the hardcoded estimate)
        expected_mb = 1024 / (1024 * 1024)
        self.assertAlmostEqual(stats.total_memory_mb, expected_mb, places=4)
        self.assertNotAlmostEqual(
            stats.total_memory_mb,
            10,
            places=0,
            msg="total_memory_mb must not equal 10 MB (hardcoded estimate)",
        )


class TestGetTotalIndexMemoryMb(unittest.TestCase):
    """get_total_index_memory_mb() in cache/__init__.py must combine HNSW + FTS."""

    def tearDown(self) -> None:
        # Reset singletons after each test
        from code_indexer.server.cache import reset_global_cache, reset_global_fts_cache

        reset_global_cache()
        reset_global_fts_cache()

    def test_get_total_index_memory_mb_is_importable(self) -> None:
        """get_total_index_memory_mb must be importable from cache module."""
        from code_indexer.server.cache import get_total_index_memory_mb

        self.assertTrue(callable(get_total_index_memory_mb))

    def test_get_total_index_memory_mb_returns_float(self) -> None:
        """get_total_index_memory_mb() must return a float."""
        from code_indexer.server.cache import get_total_index_memory_mb

        result = get_total_index_memory_mb()
        self.assertIsInstance(result, float)

    def test_get_total_index_memory_mb_returns_zero_when_caches_not_initialized(
        self,
    ) -> None:
        """get_total_index_memory_mb() must return 0.0 when caches are not initialized."""
        from code_indexer.server.cache import reset_global_cache, reset_global_fts_cache

        reset_global_cache()
        reset_global_fts_cache()

        from code_indexer.server.cache import get_total_index_memory_mb

        result = get_total_index_memory_mb()
        self.assertEqual(result, 0.0)

    def test_get_total_index_memory_mb_is_in_all_list(self) -> None:
        """get_total_index_memory_mb must be in __all__ of cache module."""
        import code_indexer.server.cache as cache_module

        self.assertIn("get_total_index_memory_mb", cache_module.__all__)

    def test_get_total_index_memory_mb_combines_hnsw_and_fts(self) -> None:
        """get_total_index_memory_mb() must return sum from both HNSW and FTS caches."""
        from code_indexer.server.cache import (
            get_global_cache,
            get_global_fts_cache,
            get_total_index_memory_mb,
            reset_global_cache,
            reset_global_fts_cache,
        )

        reset_global_cache()
        reset_global_fts_cache()

        hnsw_cache = get_global_cache()
        fts_cache = get_global_fts_cache()

        hnsw_bytes = 30 * 1024 * 1024  # 30 MB
        fts_bytes = 5 * 1024 * 1024  # 5 MB

        # Load HNSW entry
        mock_hnsw = MagicMock()
        mock_hnsw.index_file_size.return_value = hnsw_bytes

        # Load FTS entry
        mock_fts = MagicMock()
        mock_fts.reload = MagicMock()
        mock_schema = MagicMock()

        with tempfile.TemporaryDirectory() as hnsw_dir:
            with tempfile.TemporaryDirectory() as fts_dir:
                # Write FTS file of correct size
                fts_file = Path(fts_dir) / "index.dat"
                fts_file.write_bytes(b"f" * fts_bytes)

                # Override FTS cache reload_on_access to avoid reload side effects
                fts_cache.config.reload_on_access = False

                hnsw_cache.get_or_load(hnsw_dir, lambda: (mock_hnsw, {}))
                fts_cache.get_or_load(fts_dir, lambda: (mock_fts, mock_schema))

                total_mb = get_total_index_memory_mb()

        expected_total_mb = (hnsw_bytes + fts_bytes) / (1024 * 1024)
        self.assertAlmostEqual(total_mb, expected_total_mb, places=1)


class TestSystemMetricsCollectorIndexMemoryCallback(unittest.TestCase):
    """SystemMetricsCollector must support index memory provider callback."""

    def setUp(self) -> None:
        reset_system_metrics_collector()

    def tearDown(self) -> None:
        reset_system_metrics_collector()

    def test_collector_has_set_index_memory_provider_method(self) -> None:
        """SystemMetricsCollector must have set_index_memory_provider() method."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        self.assertTrue(hasattr(collector, "set_index_memory_provider"))
        self.assertTrue(callable(collector.set_index_memory_provider))
        collector.stop()

    def test_set_index_memory_provider_accepts_callable(self) -> None:
        """set_index_memory_provider() must accept a callable returning float."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)

        def provider() -> float:
            return 42.5

        # Must not raise
        collector.set_index_memory_provider(provider)
        collector.stop()

    def test_collector_has_get_index_memory_method(self) -> None:
        """SystemMetricsCollector must have get_index_memory() method."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        self.assertTrue(hasattr(collector, "get_index_memory"))
        self.assertTrue(callable(collector.get_index_memory))
        collector.stop()

    def test_get_index_memory_returns_zero_when_no_provider_set(self) -> None:
        """get_index_memory() must return 0.0 when no provider is registered."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        result = collector.get_index_memory()
        self.assertIsInstance(result, float)
        self.assertEqual(result, 0.0)
        collector.stop()

    def test_get_index_memory_uses_provider_value(self) -> None:
        """get_index_memory() must return value from registered provider."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)

        def provider() -> float:
            return 75.5

        collector.set_index_memory_provider(provider)

        # Trigger a manual cache refresh to populate index_memory_mb
        with collector._cache_lock:
            collector._refresh_cache()

        result = collector.get_index_memory()
        self.assertAlmostEqual(result, 75.5, places=1)
        collector.stop()

    def test_cached_metrics_uses_index_memory_mb_key(self) -> None:
        """After refresh, cached metrics must contain 'index_memory_mb' key (not 'mmap_total_mb')."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)

        def provider() -> float:
            return 33.0

        collector.set_index_memory_provider(provider)

        # Force refresh to ensure key is populated
        with collector._cache_lock:
            collector._refresh_cache()

        with collector._cache_lock:
            metrics = collector._cached_metrics

        self.assertIsNotNone(metrics)
        assert metrics is not None
        self.assertIn("index_memory_mb", metrics)
        self.assertNotIn("mmap_total_mb", metrics)
        collector.stop()

    def test_index_memory_provider_exception_falls_back_to_zero(self) -> None:
        """When provider raises, index_memory_mb must be 0.0 (fail-safe, not crash)."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)

        def bad_provider() -> float:
            raise RuntimeError("provider failed")

        collector.set_index_memory_provider(bad_provider)

        with collector._cache_lock:
            collector._refresh_cache()

        result = collector.get_index_memory()
        self.assertEqual(result, 0.0)
        collector.stop()


class TestSystemMetricsCollectorNoMmapKey(unittest.TestCase):
    """SystemMetricsCollector must not use mmap_total_mb key in cached_metrics."""

    def setUp(self) -> None:
        reset_system_metrics_collector()

    def tearDown(self) -> None:
        reset_system_metrics_collector()

    def test_refresh_cache_does_not_populate_mmap_total_mb(self) -> None:
        """After _refresh_cache(), cached_metrics must NOT contain 'mmap_total_mb'."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)

        with collector._cache_lock:
            collector._refresh_cache()

        with collector._cache_lock:
            metrics = collector._cached_metrics

        self.assertIsNotNone(metrics)
        assert metrics is not None
        self.assertNotIn(
            "mmap_total_mb",
            metrics,
            "mmap_total_mb key must be removed - replaced by index_memory_mb",
        )
        collector.stop()

    def test_refresh_cache_background_does_not_use_mmap_total_mb(self) -> None:
        """After _refresh_cache_background(), cached_metrics must NOT contain 'mmap_total_mb'."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)

        # Run background refresh directly
        collector._refresh_cache_background()

        with collector._cache_lock:
            metrics = collector._cached_metrics

        self.assertIsNotNone(metrics)
        assert metrics is not None
        self.assertNotIn(
            "mmap_total_mb",
            metrics,
            "mmap_total_mb key must be removed from background refresh",
        )
        collector.stop()


class TestProviderRegistrationWiring(unittest.TestCase):
    """Test the provider registration wiring pattern used in app.py."""

    def setUp(self) -> None:
        reset_system_metrics_collector()

    def tearDown(self) -> None:
        reset_system_metrics_collector()

    def test_get_total_index_memory_mb_usable_as_provider(self) -> None:
        """get_total_index_memory_mb must be usable as a provider callable for set_index_memory_provider."""
        from code_indexer.server.cache import get_total_index_memory_mb

        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)
        # Must not raise - this is exactly what app.py does
        collector.set_index_memory_provider(get_total_index_memory_mb)

        # After registering the real provider, refresh must succeed
        with collector._cache_lock:
            collector._refresh_cache()

        result = collector.get_index_memory()
        self.assertIsInstance(result, float)
        self.assertGreaterEqual(result, 0.0)
        collector.stop()

    def test_set_index_memory_provider_then_get_index_memory_returns_provider_value(
        self,
    ) -> None:
        """After registering provider via set_index_memory_provider, get_index_memory() returns its value."""
        collector = SystemMetricsCollector(cache_ttl_seconds=60.0)

        provider_value = 123.45

        def provider() -> float:
            return provider_value

        collector.set_index_memory_provider(provider)

        with collector._cache_lock:
            collector._refresh_cache()

        result = collector.get_index_memory()
        self.assertAlmostEqual(result, provider_value, places=2)
        collector.stop()


if __name__ == "__main__":
    unittest.main()
