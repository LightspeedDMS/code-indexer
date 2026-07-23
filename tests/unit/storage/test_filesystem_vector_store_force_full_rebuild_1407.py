"""Bug #1407 Amendment 2/3/5/6: end_indexing() gains force_full_rebuild and
clear_stale params for the temporal per-shard finalize barrier.

These tests drive the REAL FilesystemVectorStore + HNSWIndexManager (no
mocking of the code under test) so the durable stale-lifecycle contract is
exercised faithfully end-to-end.
"""

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager


def _points(prefix: str, n: int, dim: int = 8):
    return [
        {
            "id": f"{prefix}:{i}",
            "vector": [0.1 * (i + 1)] * dim,
            "payload": {"path": f"file_{i}.py"},
        }
        for i in range(n)
    ]


class TestEndIndexingDefaultBehaviorUnchanged:
    """Regression guard: no caller passes force_full_rebuild/clear_stale --
    end_indexing() must behave byte-identically to today for the whole
    non-temporal fleet."""

    def test_default_incremental_finalize_clears_stale(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        vector_store.create_collection("coll", 8)
        vector_store.begin_indexing("coll")
        vector_store.upsert_points("coll", _points("p", 5))

        result = vector_store.end_indexing("coll")

        assert result["status"] == "ok"
        collection_path = vector_store._get_collection_path("coll")
        hnsw_manager = HNSWIndexManager(vector_dim=8, space="cosine")
        assert hnsw_manager.is_stale(collection_path) is False

    def test_default_second_incremental_pass_still_clears_stale(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        vector_store.create_collection("coll", 8)
        vector_store.begin_indexing("coll")
        vector_store.upsert_points("coll", _points("p", 5))
        vector_store.end_indexing("coll")

        vector_store.begin_indexing("coll")
        vector_store.upsert_points("coll", _points("p2", 3))
        result = vector_store.end_indexing("coll")

        assert result["status"] == "ok"
        collection_path = vector_store._get_collection_path("coll")
        hnsw_manager = HNSWIndexManager(vector_dim=8, space="cosine")
        assert hnsw_manager.is_stale(collection_path) is False


class TestEndIndexingClearStaleFalse:
    """clear_stale=False must preserve staleness through end_indexing(),
    regardless of which internal finalize branch (incremental vs full
    rebuild) runs -- Amendment 2."""

    def test_clear_stale_false_preserves_stale_on_incremental_path(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        vector_store.create_collection("coll", 8)
        vector_store.begin_indexing("coll")
        vector_store.upsert_points("coll", _points("p", 5))
        vector_store.end_indexing("coll")  # establishes a real HNSW index

        collection_path = vector_store._get_collection_path("coll")
        hnsw_manager = HNSWIndexManager(vector_dim=8, space="cosine")
        hnsw_manager.mark_stale(collection_path)
        assert hnsw_manager.is_stale(collection_path) is True

        vector_store.begin_indexing("coll")
        vector_store.upsert_points("coll", _points("p2", 3))
        result = vector_store.end_indexing("coll", clear_stale=False)

        assert result["status"] == "ok"
        assert hnsw_manager.is_stale(collection_path) is True


class TestEndIndexingForceFullRebuild:
    """force_full_rebuild=True must bypass the incremental path and the
    _branch_isolation_did_filtered_rebuild sentinel WITHOUT consuming it,
    running a full rebuild_from_vectors() -- Amendment 3."""

    def test_force_full_rebuild_runs_full_rebuild_not_incremental(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        vector_store.create_collection("coll", 8)
        vector_store.begin_indexing("coll")
        vector_store.upsert_points("coll", _points("p", 5))

        result = vector_store.end_indexing(
            "coll", force_full_rebuild=True, clear_stale=False
        )

        assert result["status"] == "ok"
        assert "hnsw_update" not in result  # not the incremental branch
        collection_path = vector_store._get_collection_path("coll")
        hnsw_manager = HNSWIndexManager(vector_dim=8, space="cosine")
        assert hnsw_manager.index_exists(collection_path)
        # clear_stale=False -> staleness preserved (virgin shard defaults True)
        assert hnsw_manager.is_stale(collection_path) is True

    def test_force_full_rebuild_does_not_consume_foreign_sentinel(self, tmp_path):
        """The force branch must bypass _branch_isolation_did_filtered_rebuild
        WITHOUT resetting it -- a set sentinel may belong to a DIFFERENT
        collection's still-pending end_indexing (Amendment 3 implementation
        note, re-guards Bug #941)."""
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        vector_store.create_collection("coll_a", 8)
        vector_store.create_collection("coll_b", 8)

        # Simulate coll_b's rebuild_hnsw_filtered() having just set the
        # STORE-WIDE sentinel for its own still-pending end_indexing call.
        vector_store._branch_isolation_did_filtered_rebuild = True

        vector_store.begin_indexing("coll_a")
        vector_store.upsert_points("coll_a", _points("p", 5))
        vector_store.end_indexing("coll_a", force_full_rebuild=True, clear_stale=False)

        # coll_b's sentinel must survive untouched.
        assert vector_store._branch_isolation_did_filtered_rebuild is True

    def test_force_full_rebuild_with_zero_new_points_still_rescans_and_republishes(
        self, tmp_path
    ):
        """Amendment 6 healing scenario: a shard that was already published
        (real on-disk vectors) but is now physically stale, with ZERO new
        points upserted THIS run, must still get a genuine full rescan/
        republish -- not a silent no-op -- while staleness is preserved
        (clear_stale=False) for the caller's own clear_stale() to flip."""
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        vector_store.create_collection("coll", 8)
        vector_store.begin_indexing("coll")
        vector_store.upsert_points("coll", _points("p", 5))
        vector_store.end_indexing("coll")  # normal publish -> is_stale False

        collection_path = vector_store._get_collection_path("coll")
        hnsw_manager = HNSWIndexManager(vector_dim=8, space="cosine")
        stats_before = hnsw_manager.get_index_stats(collection_path)
        assert stats_before is not None
        uuid_before = stats_before["index_rebuild_uuid"]

        hnsw_manager.mark_stale(collection_path)
        assert hnsw_manager.is_stale(collection_path) is True

        vector_store.begin_indexing("coll")  # zero upserts this session
        result = vector_store.end_indexing(
            "coll", force_full_rebuild=True, clear_stale=False
        )

        assert result["status"] == "ok"
        # Staleness preserved -- only the caller's own clear_stale() may clear it.
        assert hnsw_manager.is_stale(collection_path) is True
        # A genuine rescan/republish occurred (new UUID, existing 5 vectors
        # on disk re-indexed) -- not a silent no-op.
        stats_after = hnsw_manager.get_index_stats(collection_path)
        assert stats_after is not None
        assert stats_after["index_rebuild_uuid"] != uuid_before
        assert stats_after["vector_count"] == 5
