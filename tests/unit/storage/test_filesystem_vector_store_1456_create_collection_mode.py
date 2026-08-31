"""Story #1456: create_collection() records CHUNKS_DB intent without
committing the discriminator (that commit is a MANDATORY FINAL step, only
after chunks.db + all its indexes are durable -- AC1)."""

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


class TestCreateCollectionChunksDbModeRecording:
    def test_create_collection_records_chunks_db_intent(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=32)

        assert store._chunks_db_mode.get("coll") is True

    def test_create_collection_does_not_yet_commit_discriminator(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=32)
        collection_path = store._get_collection_path("coll")

        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

    def test_default_mode_does_not_record_chunks_db_intent(self, tmp_path, monkeypatch):
        # Story #1488 superseded Bug #1486 Fix B: the CLI/daemon default is
        # SHARDED_JSON, so a default-constructed store (env unset) records
        # no CHUNKS_DB intent.
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=32)

        assert store._chunks_db_mode.get("coll") is not True
