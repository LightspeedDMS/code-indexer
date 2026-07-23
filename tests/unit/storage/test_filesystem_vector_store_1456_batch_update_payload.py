"""Story #1456 AC7: id_index.bin retirement for _batch_update_payload_only()
on CHUNKS_DB collections -- must update via ChunkStore.update_payload_fields_batch(),
never create OR consult id_index.bin.
"""

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _build_chunks_db_collection(
    store: FilesystemVectorStore, collection_name: str, records: list
):
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path(collection_name)

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    write_chunks_db_discriminator(collection_path)

    hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    hnsw_manager.rebuild_from_vectors(collection_path)

    return collection_path


def _record(point_id: str, **payload_extra) -> dict:
    payload = {"path": f"{point_id}.py"}
    payload.update(payload_extra)
    return {
        "id": point_id,
        "vector": np.random.default_rng(0)
        .standard_normal(VECTOR_DIM)
        .astype(np.float32)
        .tolist(),
        "payload": payload,
        "chunk_text": f"content {point_id}",
    }


@pytest.fixture
def store(tmp_path):
    return FilesystemVectorStore(base_path=tmp_path)


class TestBatchUpdatePayloadOnlyChunksDb:
    def test_updates_payload_field_via_chunk_store(self, store):
        records = [_record("v0", language="python")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        ok = store._batch_update_payload_only(
            [{"id": "v0", "payload": {"hidden_branches": ["feature-x"]}}],
            "coll",
        )

        assert ok is True

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            record = chunk_store.read("v0")
        finally:
            chunk_store.close()

        assert record["payload"]["hidden_branches"] == ["feature-x"]
        # Untouched fields preserved
        assert record["payload"]["language"] == "python"
        assert record["chunk_text"] == "content v0"

    def test_skips_missing_point_id_gracefully(self, store):
        records = [_record("v0")]
        _build_chunks_db_collection(store, "coll", records)

        ok = store._batch_update_payload_only(
            [{"id": "does-not-exist", "payload": {"hidden_branches": ["x"]}}],
            "coll",
        )

        assert ok is True

    def test_never_creates_id_index_bin(self, store):
        records = [_record("v0")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        store._batch_update_payload_only(
            [{"id": "v0", "payload": {"hidden_branches": ["x"]}}],
            "coll",
        )

        assert not (collection_path / "id_index.bin").exists()

    def test_ignores_a_stale_adversarial_id_index_bin(self, store, tmp_path):
        """Plant a WRONG id_index.bin mapping v0 to a nonexistent path
        BEFORE calling the update. If the method consulted id_index.bin at
        all, it would silently no-op (point not found via the bogus path).
        Proving the real chunk-store update succeeds proves it never read
        id_index.bin."""
        records = [_record("v0", language="python")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        bogus_path = tmp_path / "adversarial" / "missing.json"
        IDIndexManager().save_index(collection_path, {"v0": bogus_path})
        assert not bogus_path.exists()

        ok = store._batch_update_payload_only(
            [{"id": "v0", "payload": {"hidden_branches": ["feature-x"]}}],
            "coll",
        )
        assert ok is True

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            record = chunk_store.read("v0")
        finally:
            chunk_store.close()

        assert record["payload"]["hidden_branches"] == ["feature-x"]
