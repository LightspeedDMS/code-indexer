"""Story #1492 AC1: HNSWIndexManager.is_stale() accepts pre-parsed cached_meta.

FilesystemVectorStore.search() fetches collection_meta.json ONCE (via
CollectionMetaCache) and must be able to pass the ALREADY-PARSED dict into
is_stale() instead of it re-reading+re-parsing the file a second time. The
bare-call (no cached_meta) behavior must remain byte-identical for every
pre-existing caller (temporal indexer/incremental gate), which never pass
this new parameter.
"""

import json

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


def _manager() -> HNSWIndexManager:
    return HNSWIndexManager(vector_dim=128, space="cosine")


class TestIsStaleCachedMetaProvided:
    def test_cached_meta_fresh_index_skips_file_read(self, tmp_path):
        # No collection_meta.json written to disk at all -- if the bare-
        # read path were taken, this would fail closed to True (stale).
        cached_meta = {"hnsw_index": {"is_stale": False, "vector_count": 3}}
        assert _manager().is_stale(tmp_path, cached_meta=cached_meta) is False

    def test_cached_meta_explicit_stale_flag(self, tmp_path):
        cached_meta = {"hnsw_index": {"is_stale": True}}
        assert _manager().is_stale(tmp_path, cached_meta=cached_meta) is True

    def test_cached_meta_none_bypasses_disk_even_when_disk_says_fresh(self, tmp_path):
        # The on-disk file genuinely resolves to "not stale" (proven by the
        # bare-call assertion). Passing cached_meta=None must still resolve
        # True (needs build) -- proving disk is never consulted once
        # cached_meta is explicitly supplied.
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(
            json.dumps({"hnsw_index": {"is_stale": False, "vector_count": 0}})
        )
        assert _manager().is_stale(tmp_path) is False

        assert _manager().is_stale(tmp_path, cached_meta=None) is True


class TestIsStaleBackwardCompatibility:
    def test_no_cached_meta_arg_reads_real_file_unchanged(self, tmp_path):
        meta_path = tmp_path / "collection_meta.json"
        meta_path.write_text(
            json.dumps({"hnsw_index": {"is_stale": False, "vector_count": 5}})
        )
        assert _manager().is_stale(tmp_path) is False

        meta_path.write_text(json.dumps({"hnsw_index": {"is_stale": True}}))
        assert _manager().is_stale(tmp_path) is True
