"""clear_collection() must not leave a stale chunks_db discriminator behind.

Discovered while fixing tests/unit/cli/test_index_commits_clear_bug.py's
counting helper (Bug #1528 made temporal indexing write the CHUNKS_DB layout
by default): `FilesystemVectorStore.clear_collection()` preserves
`collection_meta.json` verbatim (to keep the projection matrix / general
metadata fast-reindex behavior), but for a CHUNKS_DB-layout collection this
also blindly preserves the `chunks_db` discriminator -- even though the
`chunks.db` file the discriminator points at was just deleted by the
preceding `shutil.rmtree()`.

That leaves the collection directory in a genuinely inconsistent state:
`collection_meta.json` claims CHUNKS_DB layout (discriminator set, per
`resolve_chunk_layout()`), but `chunks.db` itself does not exist. Any
subsequent layout-aware caller that trusts the discriminator (e.g.
`consolidate_legacy_temporal_shards()`'s pending-shard scan) then finds a
missing content-integrity manifest for what looks like an unfinished
migration and raises `UnrecoverableConsolidationCorruptionError` --
confirmed live via `cidx index --index-commits --clear` failing with
exactly that error against a freshly-built, natively-CHUNKS_DB temporal
collection.

The fix: clear_collection() must strip the `chunks_db` discriminator (and
any migration-authoritative `vector_count` cross-check field) from the
metadata it restores after the rmtree, so the collection reverts to
SHARDED_JSON-resolving (i.e. "no store built yet") -- exactly the state a
brand-new collection is in before its first `write_chunks_db_discriminator`
call, which is what re-indexing legitimately produces next.
"""

import json

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _record(point_id: str) -> dict:
    return {
        "id": point_id,
        "vector": np.random.default_rng(0)
        .standard_normal(VECTOR_DIM)
        .astype(np.float32)
        .tolist(),
        "payload": {"path": f"{point_id}.py"},
        "chunk_text": f"content {point_id}",
    }


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


@pytest.fixture
def store(tmp_path):
    return FilesystemVectorStore(base_path=tmp_path)


def test_clear_removes_chunks_db_file(store):
    records = [_record("v0"), _record("v1")]
    collection_path = _build_chunks_db_collection(store, "coll", records)
    assert (collection_path / "chunks.db").exists()

    result = store.clear_collection(collection_name="coll")

    assert result is True
    assert not (collection_path / "chunks.db").exists()


def test_clear_strips_stale_chunks_db_discriminator(store):
    """The discriminator must not survive a clear that deleted chunks.db --
    otherwise resolve_chunk_layout() lies about a store that no longer
    exists."""
    records = [_record("v0"), _record("v1")]
    collection_path = _build_chunks_db_collection(store, "coll", records)
    assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

    result = store.clear_collection(collection_name="coll")

    assert result is True
    assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON


def test_clear_preserves_other_collection_metadata(store):
    """Only the stale chunks_db discriminator is stripped -- the rest of
    collection_meta.json (name, vector_size) is preserved exactly like the
    pre-existing SHARDED_JSON clear_collection contract."""
    records = [_record("v0")]
    collection_path = _build_chunks_db_collection(store, "coll", records)

    result = store.clear_collection(collection_name="coll")

    assert result is True
    meta = json.loads((collection_path / "collection_meta.json").read_text())
    assert meta.get("name") == "coll"
    assert meta.get("vector_size") == VECTOR_DIM
    assert "chunks_db" not in meta


def test_clear_on_sharded_json_collection_is_unaffected(store):
    """A plain SHARDED_JSON collection's clear_collection behavior must stay
    byte-identical -- this fix is scoped to CHUNKS_DB collections only."""
    store.create_collection("legacy_coll", vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path("legacy_coll")
    meta_path = collection_path / "collection_meta.json"
    assert meta_path.exists()
    assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

    result = store.clear_collection(collection_name="legacy_coll")

    assert result is True
    assert meta_path.exists()
    assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON
