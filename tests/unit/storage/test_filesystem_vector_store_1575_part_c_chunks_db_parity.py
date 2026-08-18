"""TDD tests for Bug #1575 Part C -- CHUNKS_DB layout parity (AC15/AC16/
AC47: the complete SHARDED_JSON/CHUNKS_DB test matrix must be run
independently for both layouts, not merely "believed symmetric").

Mirrors the core scenarios already proven for SHARDED_JSON in
test_filesystem_vector_store_1575_part_c_decision_engine.py, now against a
real CHUNKS_DB (SQLite) collection. Real FilesystemVectorStore + real HNSW
+ real SQLite throughout -- no mocking.
"""

import json

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


class _UnusedEmbeddingProvider:
    """Placeholder passed as `embedding_provider` -- never invoked because
    every search() call below supplies `precomputed_query_vector`."""


def _vector(seed: int):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32)


def _point(point_id, path, seed):
    return {
        "id": point_id,
        "vector": _vector(seed).tolist(),
        "payload": {"path": path, "type": "content", "hidden_branches": []},
    }


def _make_store(tmp_path):
    return FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )


def _query_ids(store, collection_name, seed, limit=50):
    results = store.search(
        query="unused",
        embedding_provider=_UnusedEmbeddingProvider(),
        collection_name=collection_name,
        limit=limit,
        precomputed_query_vector=_vector(seed).tolist(),
    )
    return {r["id"] for r in results}


def _hnsw_sync(collection_path):
    meta = json.loads((collection_path / "collection_meta.json").read_text())
    return meta.get("hnsw_sync")


def _hnsw_index_identity(collection_path):
    st = (collection_path / "hnsw_index.bin").stat()
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _hnsw_index_bytes(collection_path):
    return (collection_path / "hnsw_index.bin").read_bytes()


def test_chunks_db_layout_is_recorded_in_hnsw_sync(tmp_path):
    store = _make_store(tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    result = store.end_indexing("coll")

    assert result["status"] == "ok"
    sync_state = _hnsw_sync(tmp_path / "coll")
    assert sync_state["layout"] == "chunks_db"


def test_chunks_db_unchanged_refresh_reuses_byte_for_byte(tmp_path):
    store = _make_store(tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points(
        "coll",
        [_point("v", "src/visible.py", 1), _point("h", "src/hidden.py", 2)],
    )
    store.set_hnsw_branch_context("coll", "main", {"src/visible.py"})
    _ = store.end_indexing("coll")

    assert _query_ids(store, "coll", 1) == {"v"}
    assert "h" not in _query_ids(store, "coll", 2)

    collection_path = tmp_path / "coll"
    identity_before = _hnsw_index_identity(collection_path)
    bytes_before = _hnsw_index_bytes(collection_path)

    store.begin_indexing("coll")
    store.set_hnsw_branch_context("coll", "main", {"src/visible.py"})
    result = store.end_indexing("coll")

    assert result["status"] == "ok"
    assert _hnsw_index_identity(collection_path) == identity_before
    assert _hnsw_index_bytes(collection_path) == bytes_before
    assert _query_ids(store, "coll", 1) == {"v"}
    assert "h" not in _query_ids(store, "coll", 2)


def test_chunks_db_hidden_branches_change_alone_removes_point(tmp_path):
    store = _make_store(tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points(
        "coll", [_point("a", "src/a.py", 1), _point("b", "src/b.py", 2)]
    )
    store.set_hnsw_branch_context("coll", "main", {"src/a.py", "src/b.py"})
    _ = store.end_indexing("coll")

    assert "b" in _query_ids(store, "coll", 2)

    store.begin_indexing("coll")
    store.set_hnsw_branch_context("coll", "main", {"src/a.py", "src/b.py"})
    ok = store._batch_update_payload_only(
        [{"id": "b", "payload": {"hidden_branches": ["main"]}}], "coll"
    )
    assert ok is True
    result = store.end_indexing("coll")

    assert result["status"] == "ok"
    assert "b" not in _query_ids(store, "coll", 2)
    assert _query_ids(store, "coll", 1) == {"a"}


def test_chunks_db_add_replace_delete_for_already_visible_file(tmp_path):
    store = _make_store(tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a1", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    _ = store.end_indexing("coll")
    assert _query_ids(store, "coll", 1) == {"a1"}

    # ADD a2 for the SAME already-visible file. Both a1 and a2 are upserted
    # TOGETHER: CHUNKS_DB's _upsert_points_chunks_db() applies the SAME
    # Story #540 path-based orphan-cleanup semantics as the legacy
    # SHARDED_JSON path (a re-upsert for an already-owned file path evicts
    # any point_id not present in the new batch) -- upserting a2 alone
    # would incorrectly orphan a1.
    store.begin_indexing("coll")
    _ = store.upsert_points(
        "coll", [_point("a1", "src/a.py", 1), _point("a2", "src/a.py", 3)]
    )
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    result = store.end_indexing("coll")
    assert result["status"] == "ok"
    assert "a2" in _query_ids(store, "coll", 3)
    assert "a1" in _query_ids(store, "coll", 1)

    # REPLACE a1's vector -- a2 re-supplied UNCHANGED for the same reason.
    store.begin_indexing("coll")
    _ = store.upsert_points(
        "coll", [_point("a1", "src/a.py", 4), _point("a2", "src/a.py", 3)]
    )
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    result = store.end_indexing("coll")
    assert result["status"] == "ok"
    assert "a1" in _query_ids(store, "coll", 4)
    assert "a2" in _query_ids(store, "coll", 3)

    # DELETE a2.
    store.begin_indexing("coll")
    _ = store.delete_points("coll", ["a2"])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    result = store.end_indexing("coll")
    assert result["status"] == "ok"
    assert "a2" not in _query_ids(store, "coll", 3)
    assert _query_ids(store, "coll", 4) == {"a1"}


def test_chunks_db_branch_switch_identical_visible_set_forces_full_rebuild(
    tmp_path,
):
    store = _make_store(tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    _ = store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    identity_before = _hnsw_index_identity(collection_path)

    store2 = _make_store(tmp_path)
    store2.begin_indexing("coll")
    store2.set_hnsw_branch_context("coll", "feature-x", {"src/a.py"})
    result = store2.end_indexing("coll")

    assert result["status"] == "ok"
    identity_after = _hnsw_index_identity(collection_path)
    assert identity_after != identity_before
    sync_after = _hnsw_sync(collection_path)
    assert sync_after["current_branch"] == "feature-x"


def test_chunks_db_incremental_update_reported_correctly(tmp_path):
    """CHUNKS_DB parity for the incremental-path result contract."""
    store = _make_store(tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    _ = store.end_indexing("coll")

    store.begin_indexing("coll")
    _ = store.upsert_points("coll", [_point("b", "src/b.py", 2)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py", "src/b.py"})
    result = store.end_indexing("coll")

    assert result["status"] == "ok"
    assert result.get("hnsw_update") == "incremental"
    assert "a" in _query_ids(store, "coll", 1)
    assert "b" in _query_ids(store, "coll", 2)
