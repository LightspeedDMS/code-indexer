"""Story #1456 AC3/AC7: public load_id_index() and get_all_indexed_files()
for CHUNKS_DB collections -- source from chunks.db (ChunkStore.all_point_ids
/ distinct_paths) instead of the retired id_index.bin."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _record(point_id: str, path: str) -> dict:
    return {
        "id": point_id,
        "vector": np.random.default_rng(4)
        .standard_normal(VECTOR_DIM)
        .astype(np.float32)
        .tolist(),
        "payload": {"path": path},
        "chunk_text": "x",
    }


def _seed_chunks_db(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path("coll")

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(
            [_record("v0", "a.py"), _record("v1", "a.py"), _record("v2", "b.py")]
        )
    finally:
        chunk_store.close()
    write_chunks_db_discriminator(collection_path)
    return store


class TestLoadIdIndexChunksDb:
    def test_returns_all_point_ids(self, tmp_path):
        store = _seed_chunks_db(tmp_path)

        assert store.load_id_index("coll") == {"v0", "v1", "v2"}

    def test_never_creates_id_index_bin(self, tmp_path):
        store = _seed_chunks_db(tmp_path)
        collection_path = store._get_collection_path("coll")

        store.load_id_index("coll")

        assert not (collection_path / "id_index.bin").exists()


class TestGetAllIndexedFilesChunksDb:
    def test_returns_unique_file_paths(self, tmp_path):
        store = _seed_chunks_db(tmp_path)

        assert set(store.get_all_indexed_files("coll")) == {"a.py", "b.py"}
