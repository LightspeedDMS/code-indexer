"""
Tests for Bug #881 Phase 4: HNSWIndexCacheEntry.index_size_bytes must include
the Python id_mapping dict overhead, not just the raw HNSW index file size.

The id_mapping dict (label -> vector ID) is held in Python memory for every
cached index.  For a 100k-entry index it can occupy ~10 MB.  Omitting it caused
the cache size cap to systematically under-count live memory usage.

Named constants throughout — no magic numbers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from unittest.mock import MagicMock

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
    HNSWIndexCacheEntry,
)


# Named constants — no magic numbers
HNSW_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB raw HNSW index
ID_MAPPING_ENTRIES = 1000  # entries in the label->vector-id dict
VECTOR_ID_PREFIX = "vec-"  # prefix for fake vector IDs

TTL_MINUTES = 10.0
CLEANUP_INTERVAL_SECONDS = 60

# Any is required for the HNSWIndex position in the loader return because
# HNSWIndex is a C extension (hnswlib.Index) not importable in unit tests
# without the native build.  The loader protocol only constrains the shape.
_LoaderReturn = Tuple[Any, Dict[int, str]]


def _make_id_mapping(entry_count: int) -> Dict[int, str]:
    """Return a dict with ``entry_count`` label->vector-id pairs."""
    return {i: f"{VECTOR_ID_PREFIX}{i}" for i in range(entry_count)}


def _load_entry(
    cache: HNSWIndexCache,
    loader: Callable[[], _LoaderReturn],
) -> Optional[HNSWIndexCacheEntry]:
    """Run ``loader`` through ``cache.get_or_load`` and return the cache entry.

    Encapsulates the repeated tempdir / repo_path / lock-acquire / lookup
    pattern so each test method stays focused on its single assertion.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        cache.get_or_load(tmpdir, loader)
        repo_path = str(Path(tmpdir).resolve())
        with cache._cache_lock:
            return cache._cache.get(repo_path)


def _make_cache() -> HNSWIndexCache:
    """Return a fresh HNSWIndexCache with test-safe config."""
    return HNSWIndexCache(
        config=HNSWIndexCacheConfig(
            ttl_minutes=TTL_MINUTES,
            cleanup_interval_seconds=CLEANUP_INTERVAL_SECONDS,
        )
    )


class TestIndexSizeBytesIncludesIdMappingOverhead(unittest.TestCase):
    """index_size_bytes must equal index_file_size() + sys.getsizeof(id_mapping)."""

    def setUp(self) -> None:
        self.cache = _make_cache()

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_index_size_bytes_includes_id_mapping_overhead(self) -> None:
        """entry.index_size_bytes must equal index_file_size() + sys.getsizeof(id_mapping)."""
        id_mapping = _make_id_mapping(ID_MAPPING_ENTRIES)
        expected_total = HNSW_FILE_SIZE_BYTES + sys.getsizeof(id_mapping)

        mock_index = MagicMock()
        mock_index.index_file_size.return_value = HNSW_FILE_SIZE_BYTES

        entry = _load_entry(self.cache, lambda: (mock_index, id_mapping))

        self.assertIsNotNone(entry, "Entry should be in cache after get_or_load")
        assert entry is not None
        self.assertEqual(entry.index_size_bytes, expected_total)


class TestIndexSizeBytesWithEmptyMapping(unittest.TestCase):
    """Even an empty id_mapping must have its dict overhead counted."""

    def setUp(self) -> None:
        self.cache = _make_cache()

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_index_size_bytes_with_empty_id_mapping_includes_empty_dict_overhead(
        self,
    ) -> None:
        """With empty id_mapping, index_size_bytes must still add sys.getsizeof({})."""
        empty_mapping: Dict[int, str] = {}
        expected_total = HNSW_FILE_SIZE_BYTES + sys.getsizeof(empty_mapping)

        mock_index = MagicMock()
        mock_index.index_file_size.return_value = HNSW_FILE_SIZE_BYTES

        entry = _load_entry(self.cache, lambda: (mock_index, empty_mapping))

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.index_size_bytes, expected_total)


class TestIndexSizeBytesWhenFileSizeRaises(unittest.TestCase):
    """When index_file_size() raises, id_mapping overhead is still counted."""

    def setUp(self) -> None:
        self.cache = _make_cache()

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()

    def test_index_size_bytes_when_file_size_raises_includes_only_mapping_overhead(
        self,
    ) -> None:
        """When index_file_size() raises, index_size_bytes must still include id_mapping overhead."""
        id_mapping = _make_id_mapping(ID_MAPPING_ENTRIES)
        expected_mapping_overhead = sys.getsizeof(id_mapping)

        mock_index = MagicMock()
        mock_index.index_file_size.side_effect = RuntimeError("index not on disk")

        entry = _load_entry(self.cache, lambda: (mock_index, id_mapping))

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.index_size_bytes, expected_mapping_overhead)


if __name__ == "__main__":
    unittest.main()
