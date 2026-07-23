"""Story #1456 AC3: get_file_index_timestamps(), sample_vectors(), and
validate_embedding_dimensions() for CHUNKS_DB collections -- derive from
chunks.db instead of rglob-scanning vector_*.json files."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _record(point_id: str, path: str) -> dict:
    vector = np.random.default_rng(9).standard_normal(VECTOR_DIM).astype(np.float32)
    return {
        "id": point_id,
        "vector": vector.tolist(),
        "payload": {"path": path},
        "chunk_text": "x",
    }


def _seed(tmp_path, n: int = 5) -> FilesystemVectorStore:
    """Test fixture helper: seed a CHUNKS_DB collection with n records.

    ``n`` is always a small positive literal supplied by the tests below (no
    external/untrusted input to validate). ``write_batch``/``close`` both
    have ``-> None`` signatures (raise on failure rather than returning an
    error code), so there is nothing to check here. ``FilesystemVectorStore``
    itself holds no persistent file handles/connections between calls -- each
    of its methods opens and closes its own resources -- so no cleanup is
    required for the returned instance.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path("coll")

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch([_record(f"v{i}", f"f{i}.py") for i in range(n)])
    finally:
        chunk_store.close()
    write_chunks_db_discriminator(collection_path)
    return store


class TestGetFileIndexTimestampsChunksDb:
    def test_returns_timestamp_per_file_no_crash(self, tmp_path):
        store = _seed(tmp_path)

        timestamps = store.get_file_index_timestamps("coll")

        assert set(timestamps.keys()) == {f"f{i}.py" for i in range(5)}


class TestSampleVectorsChunksDb:
    def test_samples_from_chunk_store(self, tmp_path):
        store = _seed(tmp_path)

        sample = store.sample_vectors("coll", sample_size=3)

        assert len(sample) == 3
        assert all("id" in s and "vector" in s and "file_path" in s for s in sample)


class TestValidateEmbeddingDimensionsChunksDb:
    def test_true_for_matching_dims(self, tmp_path):
        store = _seed(tmp_path)

        assert store.validate_embedding_dimensions("coll", VECTOR_DIM) is True

    def test_false_for_mismatched_dims(self, tmp_path):
        store = _seed(tmp_path)

        assert store.validate_embedding_dimensions("coll", VECTOR_DIM + 1) is False
