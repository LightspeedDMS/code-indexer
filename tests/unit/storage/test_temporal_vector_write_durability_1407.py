"""Bug #1407 Foundation: temporal vector JSON writes must be fsynced
(closing the Bug #1223 gap where filesystem_vector_store.py's upsert_points
called _atomic_write_json with the default fsync=False for ALL collections,
including temporal), and a vector file's freshly-created parent directory
must be fsynced on create so the directory entry survives a crash.

Non-temporal collections are UNCHANGED (still fsync=False) -- this is a
scoped, temporal-only durability improvement, not a fleet-wide perf
regression.
"""

from unittest.mock import patch

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _point(point_id: str, dim: int = 8):
    return {
        "id": point_id,
        "vector": [0.1] * dim,
        "payload": {"commit_hash": "abc123", "chunk_index": 0},
    }


class TestTemporalVectorFsync:
    def test_temporal_collection_upsert_fsyncs_vector_file(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        collection = "code-indexer-temporal-voyage_context_4-2024Q1"
        vector_store.create_collection(collection, 8)
        vector_store.begin_indexing(collection)

        with patch.object(
            FilesystemVectorStore, "_atomic_write_json", autospec=True
        ) as mock_write:
            vector_store.upsert_points(collection, [_point("proj:commit:abc123:0")])

        assert mock_write.called
        call = mock_write.mock_calls[0]
        args = call.args
        kwargs = call.kwargs
        fsync_value = kwargs.get("fsync", args[-1] if len(args) >= 3 else None)
        assert fsync_value is True

    def test_non_temporal_collection_upsert_does_not_fsync_vector_file(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        collection = "normal_collection"
        vector_store.create_collection(collection, 8)
        vector_store.begin_indexing(collection)

        with patch.object(
            FilesystemVectorStore, "_atomic_write_json", autospec=True
        ) as mock_write:
            vector_store.upsert_points(collection, [_point("file.py:0")])

        assert mock_write.called
        call = mock_write.mock_calls[0]
        kwargs = call.kwargs
        args = call.args
        fsync_value = kwargs.get("fsync", args[-1] if len(args) >= 3 else False)
        assert fsync_value is False


class TestTemporalDirectoryFsyncOnCreate:
    def test_new_quantization_directory_is_fsynced_for_temporal(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        collection = "code-indexer-temporal-voyage_context_4-2024Q1"
        vector_store.create_collection(collection, 8)
        vector_store.begin_indexing(collection)

        with patch(
            "code_indexer.storage.filesystem_vector_store.nfs_safe_fsync"
        ) as mock_fsync:
            vector_store.upsert_points(collection, [_point("proj:commit:abc123:0")])

        assert mock_fsync.called

    def test_non_temporal_directory_create_does_not_fsync(self, tmp_path):
        vector_store = FilesystemVectorStore(base_path=tmp_path / "index")
        collection = "normal_collection"
        vector_store.create_collection(collection, 8)
        vector_store.begin_indexing(collection)

        with patch(
            "code_indexer.storage.filesystem_vector_store.nfs_safe_fsync"
        ) as mock_fsync:
            vector_store.upsert_points(collection, [_point("file.py:0")])

        assert not mock_fsync.called
