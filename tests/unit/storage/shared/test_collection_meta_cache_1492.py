"""Unit tests for Story #1492 AC1: mtime-keyed collection_meta.json cache.

Finding C1 (SEVERE, report rank 2, "highest ROI fix in the audit"):
FilesystemVectorStore.search() previously read+parsed collection_meta.json
4-5 separate times per call. CollectionMetaCache is the shared, mtime-keyed
TTL cache (built on the EXISTING query_path_cache.TTLCache primitive from
Story #1082) that eliminates the redundant parses while staying fail-closed
and drift-safe:

- Repeated .get() calls against the SAME file (same mtime) hit the cache --
  no additional read/parse.
- A real on-disk mtime change (rewriting collection_meta.json) is observed
  immediately on the next .get() -- never served stale content beyond one
  real file mutation.
- A missing/corrupt file resolves to None (fail-closed, matching every
  existing consumer's contract) and is NEVER itself cached as a "real" key
  (a subsequent appearance of the file is picked up immediately).

Parse counts are asserted via CollectionMetaCache.counters() -- the
underlying TTLCache's OWN real hit/miss/reload bookkeeping (Story #1082
public API) -- never by monkeypatching an internal function of the module
under test. Real filesystem (temp directories), no mocking.
"""

import json
import os
import time
from pathlib import Path

from code_indexer.storage.shared.collection_meta_cache import CollectionMetaCache


def _write_meta(collection_dir: Path, data: dict) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(json.dumps(data))


def _total_reloads(cache: CollectionMetaCache) -> int:
    counters = cache.counters()
    return int(counters["immutable"]["reload"] + counters["mutable"]["reload"])


class TestCollectionMetaCacheParseCount:
    """AC1: at most one real parse per collection+mtime."""

    def test_repeated_get_same_mtime_parses_once(self, tmp_path):
        collection_dir = tmp_path / "my_collection"
        _write_meta(collection_dir, {"vector_size": 1024, "name": "my_collection"})

        cache = CollectionMetaCache()

        first = cache.get(collection_dir)
        second = cache.get(collection_dir)
        third = cache.get(collection_dir)

        assert first == {"vector_size": 1024, "name": "my_collection"}
        assert second == first
        assert third == first
        # Exactly one real read+parse (one TTLCache "reload" event) for
        # three .get() calls against an unchanged file -- this IS the AC1
        # "4-5 parses -> 1 parse" fix.
        assert _total_reloads(cache) == 1

    def test_distinct_collections_each_parse_once(self, tmp_path):
        """Multi-shard scaling: parse count scales with distinct
        collections, never with redundant re-parses of the SAME one
        (mirrors the temporal multi-shard requirement)."""
        dir_a = tmp_path / "shard_a"
        dir_b = tmp_path / "shard_b"
        _write_meta(dir_a, {"vector_size": 1024})
        _write_meta(dir_b, {"vector_size": 1536})

        cache = CollectionMetaCache()
        cache.get(dir_a)
        cache.get(dir_a)
        cache.get(dir_b)
        cache.get(dir_b)

        assert _total_reloads(cache) == 2  # one per distinct collection


class TestCollectionMetaCacheDriftSafety:
    """AC1: a real on-disk mutation is observed on the next .get()."""

    def test_mtime_change_is_observed_immediately(self, tmp_path):
        collection_dir = tmp_path / "my_collection"
        _write_meta(collection_dir, {"vector_size": 1024})

        cache = CollectionMetaCache()
        first = cache.get(collection_dir)
        assert first == {"vector_size": 1024}

        # Force a distinct mtime (some filesystems have 1s mtime
        # granularity) before rewriting with new content.
        meta_path = collection_dir / "collection_meta.json"
        time.sleep(0.01)
        new_mtime = time.time() + 5
        meta_path.write_text(json.dumps({"vector_size": 2048}))
        os.utime(meta_path, (new_mtime, new_mtime))

        second = cache.get(collection_dir)
        assert second == {"vector_size": 2048}
        # Two genuinely distinct mtimes -> two real parses (never masked
        # by TTL alone; correctness comes from the mtime being part of the
        # cache key).
        assert _total_reloads(cache) == 2

    def test_missing_file_returns_none_and_self_heals(self, tmp_path):
        collection_dir = tmp_path / "not_yet_created"

        cache = CollectionMetaCache()
        assert cache.get(collection_dir) is None

        _write_meta(collection_dir, {"vector_size": 999})
        assert cache.get(collection_dir) == {"vector_size": 999}

    def test_corrupt_json_fails_closed_to_none(self, tmp_path):
        collection_dir = tmp_path / "corrupt"
        collection_dir.mkdir(parents=True)
        (collection_dir / "collection_meta.json").write_text("{not valid json")

        cache = CollectionMetaCache()
        assert cache.get(collection_dir) is None


class TestGlobalCollectionMetaCacheSingleton:
    """Post-manual-E2E-test production fix (Story #1492 follow-up).

    A real running server was strace-verified to show ZERO cross-request
    benefit from CollectionMetaCache: every query constructs a fresh
    FilesystemVectorStore, and FilesystemVectorStore.__init__ only builds a
    CollectionMetaCache() when the caller passes None -- so every instance
    got its own private cache that died with it. get_global_collection_meta_cache()
    is the process-wide singleton getter that FilesystemBackend.get_vector_store_client()
    must inject in server mode, mirroring the established
    get_global_id_index_cache() pattern (server/cache/id_index_cache.py).
    """

    def setup_method(self) -> None:
        from code_indexer.storage.shared.collection_meta_cache import (
            reset_global_collection_meta_cache,
        )

        reset_global_collection_meta_cache()

    def teardown_method(self) -> None:
        from code_indexer.storage.shared.collection_meta_cache import (
            reset_global_collection_meta_cache,
        )

        reset_global_collection_meta_cache()

    def test_returns_same_instance_across_calls(self) -> None:
        from code_indexer.storage.shared.collection_meta_cache import (
            get_global_collection_meta_cache,
        )

        first = get_global_collection_meta_cache()
        second = get_global_collection_meta_cache()
        assert first is second

    def test_returns_a_real_collection_meta_cache_instance(self) -> None:
        from code_indexer.storage.shared.collection_meta_cache import (
            get_global_collection_meta_cache,
        )

        instance = get_global_collection_meta_cache()
        assert isinstance(instance, CollectionMetaCache)

    def test_reset_creates_a_fresh_instance(self) -> None:
        from code_indexer.storage.shared.collection_meta_cache import (
            get_global_collection_meta_cache,
            reset_global_collection_meta_cache,
        )

        first = get_global_collection_meta_cache()
        reset_global_collection_meta_cache()
        second = get_global_collection_meta_cache()
        assert first is not second
