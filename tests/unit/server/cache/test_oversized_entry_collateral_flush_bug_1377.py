"""
Regression tests for Bug #1377: oversized cache entry causes collateral cache flush.

Bug summary: HNSWIndexCache._enforce_size_limit() and FTSIndexCache._enforce_size_limit()
used a plain LRU-eviction loop whose only exit conditions were "size now <= cap" or
"cache is empty". LRU eviction always picks the least-recently-accessed entry, and the
just-inserted entry always has the newest last_accessed timestamp -- so it is evicted
LAST. If that just-inserted entry's OWN size exceeds the entire cache cap, evicting every
other (older, unrelated, individually-fitting) entry never brings the total under the cap,
so the loop keeps evicting until the cache is completely empty -- flushing perfectly good,
individually-fitting entries for zero benefit.

Fix: individually-oversized entries (index_size_bytes > cap_bytes) are evicted FIRST and
IN ISOLATION (nothing else touched), then normal LRU eviction runs on whatever remains.
An oversized entry is never retained (cold-loads every access) but must never cause
collateral damage to other, individually-fitting entries.

These tests reproduce the EXACT scenario from the bug report:
1. Load small entry A (fits well within cap).
2. Load large entry B whose OWN size exceeds the entire cap.
3. Assert A survives (was NOT evicted for zero benefit).
4. Assert A is still a cache HIT (loader not re-invoked).
5. Assert B was NOT retained (individually oversized, cannot ever fit).
6. Assert the new oversized_load_count stat reflects the oversized eviction.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)
from code_indexer.server.cache.fts_index_cache import (
    FTSIndexCache,
    FTSIndexCacheConfig,
)


class TestHNSWOversizedEntryDoesNotCollaterallyFlushCache(unittest.TestCase):
    """Bug #1377: an oversized HNSW entry must not evict unrelated, fitting entries."""

    def setUp(self) -> None:
        config = HNSWIndexCacheConfig(
            max_cache_size_mb=256, ttl_minutes=10.0, cleanup_interval_seconds=60
        )
        self.cache = HNSWIndexCache(config=config)

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_smaller_preexisting_entry_survives_oversized_load(self) -> None:
        """Loading an oversized entry B must NOT evict pre-existing, fitting entry A."""
        bytes_100mb = 100 * 1024 * 1024
        bytes_300mb = 300 * 1024 * 1024

        mock_index_a = MagicMock()
        mock_index_a.index_file_size.return_value = bytes_100mb

        mock_index_b = MagicMock()
        mock_index_b.index_file_size.return_value = bytes_300mb

        with tempfile.TemporaryDirectory() as dir_a:
            with tempfile.TemporaryDirectory() as dir_b:

                def loader_a() -> Tuple[Any, Dict]:
                    return mock_index_a, {}

                def loader_b() -> Tuple[Any, Dict]:
                    return mock_index_b, {}

                # Step 1: load A (100MB, fits comfortably within the 256MB cap)
                self.cache.get_or_load(dir_a, loader_a)

                # Step 2: load B (300MB, exceeds the ENTIRE cap on its own)
                self.cache.get_or_load(dir_b, loader_b)

                path_a = str(Path(dir_a).resolve())
                path_b = str(Path(dir_b).resolve())

                with self.cache._cache_lock:
                    cache_keys = set(self.cache._cache.keys())

                # Step 4: A must STILL be in the cache -- it was not evicted for
                # zero benefit (documents the collateral-flush regression directly).
                self.assertIn(
                    path_a,
                    cache_keys,
                    "Entry A (individually fits) must survive loading oversized entry B",
                )

                # Step 7: B must NOT be retained -- it is individually oversized and
                # can never be made to fit, so it must cold-load on every access.
                self.assertNotIn(
                    path_b,
                    cache_keys,
                    "Oversized entry B must not be retained in the cache",
                )

                # Documents old-behavior regression directly: cache must not be empty.
                self.assertGreaterEqual(
                    len(cache_keys),
                    1,
                    "Cache must not be flushed empty by an oversized entry",
                )

                def loader_a_should_not_be_called() -> Tuple[Any, Dict]:
                    raise AssertionError("A should be a cache HIT, loader must not run")

                # Step 5: A must be a cache HIT -- re-requesting it must succeed
                # without invoking the loader (which would raise if called).
                stats_before = self.cache.get_stats()
                self.cache.get_or_load(dir_a, loader_a_should_not_be_called)
                stats_after = self.cache.get_stats()

                # Step 6: hit_count must have increased by exactly 1.
                self.assertEqual(
                    stats_after.hit_count,
                    stats_before.hit_count + 1,
                    "Re-accessing A must be a cache HIT, not a cold reload",
                )

    def test_oversized_load_count_stat_increments(self) -> None:
        """Step 8: get_stats().oversized_load_count must be 1 after loading oversized B."""
        bytes_100mb = 100 * 1024 * 1024
        bytes_300mb = 300 * 1024 * 1024

        mock_index_a = MagicMock()
        mock_index_a.index_file_size.return_value = bytes_100mb

        mock_index_b = MagicMock()
        mock_index_b.index_file_size.return_value = bytes_300mb

        with tempfile.TemporaryDirectory() as dir_a:
            with tempfile.TemporaryDirectory() as dir_b:

                def loader_a() -> Tuple[Any, Dict]:
                    return mock_index_a, {}

                def loader_b() -> Tuple[Any, Dict]:
                    return mock_index_b, {}

                self.cache.get_or_load(dir_a, loader_a)
                self.cache.get_or_load(dir_b, loader_b)

                stats = self.cache.get_stats()
                self.assertEqual(
                    stats.oversized_load_count,
                    1,
                    "oversized_load_count must be 1 after one oversized eviction",
                )


class TestFTSOversizedEntryDoesNotCollaterallyFlushCache(unittest.TestCase):
    """Bug #1377: an oversized FTS entry must not evict unrelated, fitting entries."""

    def setUp(self) -> None:
        config = FTSIndexCacheConfig(
            max_cache_size_mb=256,
            ttl_minutes=10.0,
            cleanup_interval_seconds=60,
            reload_on_access=False,
        )
        self.cache = FTSIndexCache(config=config)

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_smaller_preexisting_entry_survives_oversized_load(self) -> None:
        """Loading an oversized FTS entry B must NOT evict pre-existing, fitting entry A."""
        mb_100 = 100 * 1024 * 1024
        mb_300 = 300 * 1024 * 1024

        mock_index_a = MagicMock()
        mock_index_a.reload = MagicMock()
        mock_schema_a = MagicMock()

        mock_index_b = MagicMock()
        mock_index_b.reload = MagicMock()
        mock_schema_b = MagicMock()

        with tempfile.TemporaryDirectory() as dir_a:
            with tempfile.TemporaryDirectory() as dir_b:
                # Write real files sized to trigger get_or_load's directory-scan
                # size computation (FTS computes size via Path.rglob, not a mock).
                (Path(dir_a) / "data.bin").write_bytes(b"a" * mb_100)
                (Path(dir_b) / "data.bin").write_bytes(b"b" * mb_300)

                def loader_a() -> Tuple[Any, Any]:
                    return mock_index_a, mock_schema_a

                def loader_b() -> Tuple[Any, Any]:
                    return mock_index_b, mock_schema_b

                # Step 1: load A (100MB, fits comfortably within the 256MB cap)
                self.cache.get_or_load(dir_a, loader_a)

                # Step 2: load B (300MB, exceeds the ENTIRE cap on its own)
                self.cache.get_or_load(dir_b, loader_b)

                path_a = str(Path(dir_a).resolve())
                path_b = str(Path(dir_b).resolve())

                with self.cache._cache_lock:
                    cache_keys = set(self.cache._cache.keys())

                # Step 4: A must STILL be in the cache -- it was not evicted for
                # zero benefit (documents the collateral-flush regression directly).
                self.assertIn(
                    path_a,
                    cache_keys,
                    "Entry A (individually fits) must survive loading oversized entry B",
                )

                # Step 7: B must NOT be retained -- it is individually oversized and
                # can never be made to fit, so it must cold-load on every access.
                self.assertNotIn(
                    path_b,
                    cache_keys,
                    "Oversized entry B must not be retained in the cache",
                )

                # Documents old-behavior regression directly: cache must not be empty.
                self.assertGreaterEqual(
                    len(cache_keys),
                    1,
                    "Cache must not be flushed empty by an oversized entry",
                )

                def loader_a_should_not_be_called() -> Tuple[Any, Any]:
                    raise AssertionError("A should be a cache HIT, loader must not run")

                # Step 5: A must be a cache HIT -- re-requesting it must succeed
                # without invoking the loader (which would raise if called).
                stats_before = self.cache.get_stats()
                self.cache.get_or_load(dir_a, loader_a_should_not_be_called)
                stats_after = self.cache.get_stats()

                # Step 6: hit_count must have increased by exactly 1.
                self.assertEqual(
                    stats_after.hit_count,
                    stats_before.hit_count + 1,
                    "Re-accessing A must be a cache HIT, not a cold reload",
                )

    def test_oversized_load_count_stat_increments(self) -> None:
        """Step 8: get_stats().oversized_load_count must be 1 after loading oversized B."""
        mb_100 = 100 * 1024 * 1024
        mb_300 = 300 * 1024 * 1024

        mock_index_a = MagicMock()
        mock_index_a.reload = MagicMock()
        mock_schema_a = MagicMock()

        mock_index_b = MagicMock()
        mock_index_b.reload = MagicMock()
        mock_schema_b = MagicMock()

        with tempfile.TemporaryDirectory() as dir_a:
            with tempfile.TemporaryDirectory() as dir_b:
                (Path(dir_a) / "data.bin").write_bytes(b"a" * mb_100)
                (Path(dir_b) / "data.bin").write_bytes(b"b" * mb_300)

                def loader_a() -> Tuple[Any, Any]:
                    return mock_index_a, mock_schema_a

                def loader_b() -> Tuple[Any, Any]:
                    return mock_index_b, mock_schema_b

                self.cache.get_or_load(dir_a, loader_a)
                self.cache.get_or_load(dir_b, loader_b)

                stats = self.cache.get_stats()
                self.assertEqual(
                    stats.oversized_load_count,
                    1,
                    "oversized_load_count must be 1 after one oversized eviction",
                )


if __name__ == "__main__":
    unittest.main()
