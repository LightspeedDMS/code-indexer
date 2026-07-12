"""
Tests for EVO-64244: HNSWIndexCache must not negatively-cache a missing index.

Facet 1 (critical): a loader that returns (None, id_mapping) because
hnsw_index.bin does not exist yet must NOT be stored in the cache. A later
query whose loader now returns a real index must pick it up immediately,
without waiting for the TTL to expire or the pod to restart.

Facet 2: a successfully-cached real index goes stale when the repo is
re-indexed (hnsw_index.bin is atomically replaced). When get_or_load is
given the index_file path, a newer on-disk mtime on a cache HIT must
invalidate the stale in-RAM object and reload.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)


class TestHNSWNegativeCacheEVO64244(unittest.TestCase):
    """get_or_load must never serve a negatively-cached (None) index."""

    def setUp(self) -> None:
        config = HNSWIndexCacheConfig(ttl_minutes=10.0, cleanup_interval_seconds=60)
        self.cache = HNSWIndexCache(config=config)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.repo_path = str(Path(self.tmpdir.name) / "collection")

    def tearDown(self) -> None:
        self.cache.stop_background_cleanup()
        self.tmpdir.cleanup()

    def _real_index(self, size: int = 1000) -> MagicMock:
        index = MagicMock()
        index.index_file_size.return_value = size
        return index

    def test_none_index_not_cached_then_real_index_served(self) -> None:
        """A loader returning (None, ...) is not cached; the next call reloads.

        Facet 1: first the index is missing (loader -> None); nothing is
        cached. On the second call the graph is built (loader -> real index)
        and it is returned immediately, proving the None was never cached.
        """
        calls = {"n": 0}
        real_index = self._real_index()

        def loader():
            calls["n"] += 1
            if calls["n"] == 1:
                return None, {0: "v0"}
            return real_index, {0: "v0"}

        idx, mapping = self.cache.get_or_load(self.repo_path, loader)
        self.assertIsNone(idx)
        self.assertEqual(mapping, {0: "v0"})
        # The None result must NOT have been stored.
        self.assertEqual(self.cache.get_stats().cached_repositories, 0)

        # Index is now built: a second call re-runs the loader and serves it.
        idx2, _ = self.cache.get_or_load(self.repo_path, loader)
        self.assertIs(idx2, real_index)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.cache.get_stats().cached_repositories, 1)

    def test_real_index_cached_and_served_on_hit(self) -> None:
        """A real index is cached; a second call is served without reloading."""
        calls = {"n": 0}
        real_index = self._real_index()

        def loader():
            calls["n"] += 1
            return real_index, {0: "v0"}

        idx, _ = self.cache.get_or_load(self.repo_path, loader)
        idx2, _ = self.cache.get_or_load(self.repo_path, loader)
        self.assertIs(idx, real_index)
        self.assertIs(idx2, real_index)
        self.assertEqual(calls["n"], 1)  # served from cache on the second call

    def test_newer_ondisk_mtime_triggers_reload(self) -> None:
        """Facet 2: a rebuilt (newer-mtime) on-disk index invalidates the entry."""
        index_file = Path(self.tmpdir.name) / "hnsw_index.bin"
        index_file.write_bytes(b"v1")
        old_time = time.time() - 100
        os.utime(index_file, (old_time, old_time))

        calls = {"n": 0}
        index_v1 = self._real_index(1000)
        index_v2 = self._real_index(2000)

        def loader():
            calls["n"] += 1
            return (index_v1 if calls["n"] == 1 else index_v2), {0: "v0"}

        idx1, _ = self.cache.get_or_load(self.repo_path, loader, index_file=index_file)
        self.assertIs(idx1, index_v1)
        self.assertEqual(calls["n"], 1)

        # Repo re-indexed: on-disk file is atomically replaced (newer mtime).
        new_time = time.time() + 100
        os.utime(index_file, (new_time, new_time))

        idx2, _ = self.cache.get_or_load(self.repo_path, loader, index_file=index_file)
        self.assertIs(idx2, index_v2)  # stale entry evicted and reloaded
        self.assertEqual(calls["n"], 2)

    def test_unchanged_mtime_serves_cached_entry(self) -> None:
        """Facet 2: an unchanged on-disk mtime keeps serving the cached entry."""
        index_file = Path(self.tmpdir.name) / "hnsw_index.bin"
        index_file.write_bytes(b"v1")

        calls = {"n": 0}
        real_index = self._real_index()

        def loader():
            calls["n"] += 1
            return real_index, {0: "v0"}

        idx1, _ = self.cache.get_or_load(self.repo_path, loader, index_file=index_file)
        idx2, _ = self.cache.get_or_load(self.repo_path, loader, index_file=index_file)
        self.assertIs(idx1, real_index)
        self.assertIs(idx2, real_index)
        self.assertEqual(calls["n"], 1)  # no reload when mtime is unchanged

    def test_missing_index_file_does_not_crash_on_hit(self) -> None:
        """Facet 2: a missing index_file on HIT falls back to the cached entry."""
        index_file = Path(self.tmpdir.name) / "does_not_exist.bin"

        calls = {"n": 0}
        real_index = self._real_index()

        def loader():
            calls["n"] += 1
            return real_index, {0: "v0"}

        idx1, _ = self.cache.get_or_load(self.repo_path, loader, index_file=index_file)
        idx2, _ = self.cache.get_or_load(self.repo_path, loader, index_file=index_file)
        self.assertIs(idx1, real_index)
        self.assertIs(idx2, real_index)  # served cached, no crash
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
