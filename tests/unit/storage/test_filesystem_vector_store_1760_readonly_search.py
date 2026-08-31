"""Bug #1760 (Fix 1/2) -- end-to-end proof that FilesystemVectorStore.search()
tolerates a non-writable chunks.db for a CHUNKS_DB collection.

This is the full production entry point (not just the cache-layer unit
tests in test_chunk_store_thread_cache_1760_readonly.py): a real
FilesystemVectorStore, a real on-disk CHUNKS_DB collection (chunks.db +
HNSW index + discriminator), forced into the exact confirmed-production
failure shape -- chunks.db left in persisted WAL journal mode (as ANY
prior tool/process/library opening it, even transiently, would leave it),
then made non-writable -- and a real `search()` call via
`precomputed_query_vector` (no embedding-provider network call needed).

Before the fix, search()'s CHUNKS_DB hydration path opens chunks.db
through `ChunkStoreThreadCache.get_or_open()` with its default mutable
mode, which unconditionally re-asserts `PRAGMA journal_mode=DELETE` --
a genuine write against the persisted WAL mode, raising
`sqlite3.OperationalError: attempt to write a readonly database` (byte-
identical to the confirmed production log) instead of returning results.
"""

import os
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


class _UnusedEmbeddingProvider:
    """Never invoked -- precomputed_query_vector skips generate_embedding()."""


def _record(point_id: str, vector: np.ndarray) -> dict:
    return {
        "id": point_id,
        "vector": vector.astype(np.float32).tolist(),
        "payload": {"path": f"{point_id}.py"},
        "chunk_text": f"content for {point_id}",
    }


def _build_chunks_db_collection(
    store: FilesystemVectorStore, collection_name: str, records: list
) -> Path:
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = Path(store._get_collection_path(collection_name))

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    write_chunks_db_discriminator(collection_path)
    HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine").rebuild_from_vectors(
        collection_path
    )
    return collection_path


def _force_wal_mode(db_path: Path) -> sqlite3.Connection:
    """Switch to WAL mode and perform a REAL write, keeping the connection
    OPEN so the resulting ``-wal``/``-shm`` files are materialized and
    persist on disk -- not merely a persisted ``journal_mode=wal`` header
    with no actual WAL file.

    Empirically proven (real sqlite3, no mocking; see
    test_chunk_store_thread_cache_1760_readonly.py's
    ``readonly_collection_with_wal_content`` fixture for the full
    rationale): SQLite's ``mode=ro`` needs to CREATE a ``-shm`` file the
    first time anything reads a WAL-mode database, which requires
    directory write access -- so an empty-WAL precondition genuinely
    cannot be opened via ``mode=ro`` on a fully read-only directory,
    regardless of the fix. When ``-shm``/``-wal`` already exist on disk,
    ``mode=ro`` can open and use them without creating anything, so it
    succeeds even with a fully read-only file+directory. This is also the
    REALISTIC production precondition: a collection actively held open by
    a long-lived indexing/refresh WAL writer connection always has a
    materialized ``-shm``/``-wal`` pair on disk.

    Returns the open connection -- the caller MUST close it AFTER
    restoring write permissions (closing the last WAL connection
    auto-checkpoints, which needs write access).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode == ("wal",), f"failed to persist WAL mode, got {mode!r}"
        conn.execute("UPDATE chunks SET path = path")
        conn.commit()
        assert os.path.exists(str(db_path) + "-wal"), (
            "test setup invalid: -wal file was not materialized"
        )
        assert os.path.exists(str(db_path) + "-shm"), (
            "test setup invalid: -shm file was not materialized"
        )
    except BaseException:
        conn.close()
        raise
    return conn


def _make_readonly(collection_path: Path, db_path: Path) -> None:
    os.chmod(db_path, 0o444)
    os.chmod(collection_path, 0o555)


def _restore_writable(collection_path: Path, db_path: Path) -> None:
    os.chmod(collection_path, 0o755)
    os.chmod(db_path, 0o644)


@pytest.fixture
def rng():
    return np.random.default_rng(1760)


class TestSearchToleratesReadonlyChunksDb:
    def test_search_returns_results_against_wal_readonly_chunks_db(self, tmp_path, rng):
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        records = [_record(f"vec_{i:04d}", v) for i, v in enumerate(vectors)]
        collection_path = _build_chunks_db_collection(store, "coll", records)
        db_path = collection_path / "chunks.db"

        # Any: store.search() returns Union[List[Dict], Tuple[List[Dict],
        # Dict]] (the tuple variant only when return_timing=True, never
        # passed here) -- a plain `list` annotation cannot represent that
        # union, so mypy rejects the assignment below without this.
        results: Any = []
        wal_conn = _force_wal_mode(db_path)
        try:
            _make_readonly(collection_path, db_path)
            try:
                results = store.search(
                    query="unused",
                    embedding_provider=_UnusedEmbeddingProvider(),
                    collection_name="coll",
                    limit=3,
                    precomputed_query_vector=vectors[0].tolist(),
                )
            finally:
                _restore_writable(collection_path, db_path)
        finally:
            wal_conn.close()

        # return_timing was never passed, so this is always a plain list --
        # narrows the Union[List[Dict], Tuple[List[Dict], Dict]] return type
        # for mypy's --check-untyped-defs pass.
        assert isinstance(results, list)
        assert len(results) > 0
        assert results[0]["id"] == "vec_0000"
