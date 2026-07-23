"""Story #1456: _is_chunks_db_collection() combines the in-session build
intent (create_collection's recorded _chunks_db_mode, needed while a fresh
build is in progress BEFORE the discriminator exists) with the durable
resolver (for a collection consolidated in a PRIOR session)."""

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator


class TestIsChunksDbCollectionHelper:
    def test_true_when_session_recorded_intent(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=32)
        collection_path = store._get_collection_path("coll")

        assert store._is_chunks_db_collection("coll", collection_path) is True

    def test_true_when_prior_session_already_committed_discriminator(self, tmp_path):
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=32)
        collection_path = store._get_collection_path("coll")
        write_chunks_db_discriminator(collection_path)

        fresh_store = FilesystemVectorStore(base_path=tmp_path)
        assert fresh_store._is_chunks_db_collection("coll", collection_path) is True

    def test_false_for_ordinary_sharded_collection(self, tmp_path):
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=32)
        collection_path = store._get_collection_path("coll")

        assert store._is_chunks_db_collection("coll", collection_path) is False
