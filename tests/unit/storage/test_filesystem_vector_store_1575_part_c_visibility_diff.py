"""TDD tests for Bug #1575 Part C -- ``_batch_update_payload_only()``
registering ``visibility_changed`` on the collection's ``HNSWSyncSession``
(AC12/AC43) and bumping the dirty mutation epoch, for BOTH storage layouts.

Real filesystem/SQLite via ``FilesystemVectorStore`` -- no mocking.
"""

import json

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 16


def _vector(seed: int = 0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


def _points(paths, prefix="p"):
    return [
        {
            "id": f"{prefix}_{i}",
            "vector": _vector(i),
            "payload": {"path": path, "type": "content", "hidden_branches": []},
        }
        for i, path in enumerate(paths)
    ]


def _make_store(tmp_path, chunks_db: bool):
    return FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=chunks_db
    )


def _session_for(store, collection_path):
    key = store._hnsw_sync_session_key(collection_path)
    return store._hnsw_sync_sessions[key]


def _read_hnsw_sync(collection_path):
    meta = json.loads((collection_path / "collection_meta.json").read_text())
    return meta["hnsw_sync"]


@pytest.mark.parametrize("chunks_db", [False, True], ids=["sharded_json", "chunks_db"])
def test_hidden_branches_change_registers_visibility_changed(tmp_path, chunks_db):
    store = _make_store(tmp_path, chunks_db)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", _points(["src/a.py"]))

    epoch_before = _read_hnsw_sync(tmp_path / "coll")["mutation_epoch"]

    store._batch_update_payload_only(
        [{"id": "p_0", "payload": {"hidden_branches": ["feature-x"]}}], "coll"
    )

    session = _session_for(store, tmp_path / "coll")
    assert "p_0" in session.visibility_changed
    epoch_after = _read_hnsw_sync(tmp_path / "coll")["mutation_epoch"]
    assert epoch_after > epoch_before


@pytest.mark.parametrize("chunks_db", [False, True], ids=["sharded_json", "chunks_db"])
def test_no_op_merge_does_not_register_visibility_changed_but_still_bumps_epoch(
    tmp_path, chunks_db
):
    store = _make_store(tmp_path, chunks_db)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", _points(["src/a.py"]))

    epoch_before = _read_hnsw_sync(tmp_path / "coll")["mutation_epoch"]

    # Merge the SAME (empty) hidden_branches value that's already stored.
    store._batch_update_payload_only(
        [{"id": "p_0", "payload": {"hidden_branches": []}}], "coll"
    )

    session = _session_for(store, tmp_path / "coll")
    assert "p_0" not in session.visibility_changed
    # Every mutation ENTRY POINT call bumps the epoch regardless of whether
    # it turned out to be a real visibility change -- the dirty-before-write
    # protocol runs BEFORE the merge is even computed.
    epoch_after = _read_hnsw_sync(tmp_path / "coll")["mutation_epoch"]
    assert epoch_after > epoch_before


@pytest.mark.parametrize("chunks_db", [False, True], ids=["sharded_json", "chunks_db"])
def test_path_change_registers_visibility_changed(tmp_path, chunks_db):
    store = _make_store(tmp_path, chunks_db)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", _points(["src/old.py"]))

    store._batch_update_payload_only(
        [{"id": "p_0", "payload": {"path": "src/new.py"}}], "coll"
    )

    session = _session_for(store, tmp_path / "coll")
    assert "p_0" in session.visibility_changed
