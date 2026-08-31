"""TDD tests for Bug #1575 Part C -- the core dirty-before-mutation
mechanism wired into ``upsert_points()``/``delete_points()``.

Real filesystem I/O via ``FilesystemVectorStore`` + ``tmp_path`` throughout
-- no mocking. Covers only the FOUNDATIONAL epoch-bump mechanism here (plus
its immediate companions: layout recording and abort_indexing() cleanup);
the full decision-engine / incremental-apply / branch-context behavior is
covered by sibling test files in this directory.
"""

import json

import numpy as np

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
            "payload": {"path": path, "type": "content"},
        }
        for i, path in enumerate(paths)
    ]


def _read_hnsw_sync(collection_path):
    """Bug #1619: resolve hnsw_sync the same way production code does --
    prefer the dedicated hnsw_sync_state.json file, falling back to the
    legacy embedded collection_meta.json key for pre-migration collections."""
    sync_file = collection_path / "hnsw_sync_state.json"
    if sync_file.exists():
        return json.loads(sync_file.read_text())
    meta_file = collection_path / "collection_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text())
    return meta.get("hnsw_sync")


def test_first_upsert_creates_dirty_hnsw_sync_state(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")

    assert _read_hnsw_sync(tmp_path / "coll") is None

    store.upsert_points("coll", _points(["src/a.py"]))

    sync_state = _read_hnsw_sync(tmp_path / "coll")
    assert sync_state is not None
    assert sync_state["mutation_epoch"] == 1
    assert sync_state["published_epoch"] == 0
    assert sync_state["status"] == "dirty"
    assert sync_state["layout"] == "sharded_json"


def test_second_mutation_bumps_epoch_further(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")

    store.upsert_points("coll", _points(["src/a.py"]))
    store.upsert_points("coll", _points(["src/b.py"], prefix="q"))

    sync_state = _read_hnsw_sync(tmp_path / "coll")
    assert sync_state["mutation_epoch"] == 2
    assert sync_state["published_epoch"] == 0
    assert sync_state["status"] == "dirty"


def test_delete_points_also_bumps_epoch(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", _points(["src/a.py"]))

    epoch_after_upsert = _read_hnsw_sync(tmp_path / "coll")["mutation_epoch"]

    store.delete_points("coll", ["p_0"])

    epoch_after_delete = _read_hnsw_sync(tmp_path / "coll")["mutation_epoch"]
    assert epoch_after_delete > epoch_after_upsert


def test_two_collections_have_independent_epochs(tmp_path):
    """AC14: one collection's mutation/epoch state must be structurally
    unable to affect a different collection's."""
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll-a", vector_size=VECTOR_DIM)
    store.create_collection("coll-b", vector_size=VECTOR_DIM)
    store.begin_indexing("coll-a")
    store.begin_indexing("coll-b")

    store.upsert_points("coll-a", _points(["src/a.py"]))
    store.upsert_points("coll-a", _points(["src/a2.py"], prefix="q"))
    store.upsert_points("coll-b", _points(["src/b.py"]))

    sync_a = _read_hnsw_sync(tmp_path / "coll-a")
    sync_b = _read_hnsw_sync(tmp_path / "coll-b")

    assert sync_a["mutation_epoch"] == 2
    assert sync_b["mutation_epoch"] == 1


def test_hnsw_sync_epoch_disabled_never_writes_hnsw_sync_key(tmp_path):
    """AC46 fail-closed gate: hnsw_sync_epoch_enabled=False must make the
    ENTIRE mechanism a no-op."""
    store = FilesystemVectorStore(base_path=tmp_path, hnsw_sync_epoch_enabled=False)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")

    store.upsert_points("coll", _points(["src/a.py"]))
    store.delete_points("coll", ["p_0"])

    assert _read_hnsw_sync(tmp_path / "coll") is None


def test_chunks_db_layout_recorded_correctly(tmp_path):
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")

    store.upsert_points("coll", _points(["src/a.py"]))

    sync_state = _read_hnsw_sync(tmp_path / "coll")
    assert sync_state["layout"] == "chunks_db"


def test_abort_indexing_discards_in_memory_session_without_touching_disk_state(
    tmp_path,
):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", _points(["src/a.py"]))

    disk_state_before = _read_hnsw_sync(tmp_path / "coll")

    store.abort_indexing("coll")

    disk_state_after = _read_hnsw_sync(tmp_path / "coll")
    assert disk_state_after == disk_state_before

    key = store._hnsw_sync_session_key(tmp_path / "coll")
    assert key not in store._hnsw_sync_sessions
