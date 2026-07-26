"""Code review finding #3 (non-blocking, consistency): get_point(),
delete_points(), and _batch_update_payload_only() must resolve CHUNKS_DB
via the combined _is_chunks_db_collection authority (in-session build intent
OR the durable discriminator) -- not the bare resolve_chunk_layout(), which
cannot see a fresh build's chunks.db until end_indexing() commits the
discriminator as its mandatory FINAL step. This is the exact same mid-build
window that produced the two real bugs (_save_path_index,
rebuild_hnsw_filtered) already fixed earlier in this story."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


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


def _build_mid_construction_chunks_db_collection(tmp_path, records):
    """Fresh build IN PROGRESS: chunks_db_mode intent recorded, chunks.db
    written, but end_indexing() has NOT run yet -- the discriminator is
    deliberately NOT committed, mirroring the real window during a `cidx
    index` run."""
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path("coll")

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    return store, collection_path


class TestGetPointMidBuildConsistency:
    def test_resolves_via_chunk_store_before_discriminator_committed(self, tmp_path):
        store, _ = _build_mid_construction_chunks_db_collection(
            tmp_path, [_record("v0")]
        )

        result = store.get_point("v0", "coll")

        assert result is not None
        assert result["id"] == "v0"


class TestDeletePointsMidBuildConsistency:
    def test_deletes_via_chunk_store_before_discriminator_committed(self, tmp_path):
        store, collection_path = _build_mid_construction_chunks_db_collection(
            tmp_path, [_record("v0"), _record("v1")]
        )

        result = store.delete_points("coll", ["v0"])

        assert result["status"] == "ok"
        assert result["deleted"] == 1

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            assert chunk_store.read("v0") is None
            assert chunk_store.read("v1") is not None
        finally:
            chunk_store.close()


class TestBatchUpdatePayloadOnlyMidBuildConsistency:
    def test_updates_via_chunk_store_before_discriminator_committed(self, tmp_path):
        store, collection_path = _build_mid_construction_chunks_db_collection(
            tmp_path, [_record("v0", language="python")]
        )

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
