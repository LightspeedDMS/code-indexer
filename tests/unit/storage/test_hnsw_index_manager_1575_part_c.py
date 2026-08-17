"""TDD tests for Bug #1575 Part C -- HNSWIndexManager additions:

1. ``lock_already_held`` on ``BackgroundIndexRebuilder.rebuild_with_lock()``
   and ``HNSWIndexManager.rebuild_from_vectors()`` -- so end_indexing()'s
   Part C decision engine can hold ``.index_rebuild.lock`` once for the
   whole decision and delegate to a full rebuild without a nested
   self-deadlock.
2. ``save_incremental_update()`` preserving ``filtered``/``current_branch``/
   ``visible_count``/``total_on_disk`` instead of wiping them on every
   incremental publish (a real, verified pre-existing gap).
3. ``validate_hnsw_artifact_for_reuse()`` -- the loadability/consistency
   check gating the "epoch clean -> reuse byte-for-byte" fast path.

RED phase: tests exercising NEW capability (the ``lock_already_held``
keyword argument, ``validate_hnsw_artifact_for_reuse``, and the
gap-closing metadata-preservation assertions) must FAIL against pre-Part-C
code with a TypeError/AttributeError/AssertionError. A few tests are
explicit REGRESSION GUARDS proving pre-existing default behavior is
unchanged (named ``*_default_still_*``/``*_stays_unfiltered``) -- those
already pass today and must keep passing after the change too.

Real hnswlib/filesystem I/O throughout -- no mocking of the HNSW index or
storage under test. ``FilesystemVectorStore`` is used only to build a
realistic on-disk fixture (real vector files + a real HNSW index), matching
the conventions of the existing Part A/B test files in this directory.
"""

import fcntl
import json

import numpy as np
import pytest

from code_indexer.storage.background_index_rebuilder import BackgroundIndexRebuilder
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock

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


def _build_real_collection(tmp_path, paths):
    """Build a real collection with a real HNSW index via the actual
    indexing lifecycle (create_collection -> begin_indexing -> upsert_points
    -> end_indexing), matching production code paths exactly."""
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", _points(paths))
    store.end_indexing("coll")
    return tmp_path / "coll"


# --- BackgroundIndexRebuilder.rebuild_with_lock(lock_already_held=True) ----


@pytest.mark.timeout(10)
def test_rebuild_with_lock_already_held_does_not_deadlock(tmp_path):
    collection_path = tmp_path / "coll"
    collection_path.mkdir()
    rebuilder = BackgroundIndexRebuilder(collection_path)
    target_file = collection_path / "some_index.bin"

    def build_fn(temp_file):
        temp_file.write_bytes(b"payload")

    with rebuilder.acquire_lock():
        # Must complete WITHOUT trying to re-acquire .index_rebuild.lock --
        # doing so on a fresh fd would self-deadlock (flock is per open file
        # description, not per-process).
        rebuilder.rebuild_with_lock(build_fn, target_file, lock_already_held=True)

    assert target_file.read_bytes() == b"payload"


@pytest.mark.timeout(10)
def test_rebuild_with_lock_default_still_acquires_lock(tmp_path):
    """REGRESSION GUARD: byte-identical default behavior for every
    pre-existing caller (already passes today)."""
    collection_path = tmp_path / "coll"
    collection_path.mkdir()
    rebuilder = BackgroundIndexRebuilder(collection_path)
    target_file = collection_path / "some_index.bin"

    def build_fn(temp_file):
        temp_file.write_bytes(b"payload2")

    rebuilder.rebuild_with_lock(build_fn, target_file)

    assert target_file.read_bytes() == b"payload2"


# --- HNSWIndexManager.rebuild_from_vectors(lock_already_held=True) ---------


@pytest.mark.timeout(10)
def test_rebuild_from_vectors_lock_already_held_does_not_deadlock(tmp_path):
    collection_path = _build_real_collection(tmp_path, ["src/a.py", "src/b.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")

    lock_file = collection_path / ".index_rebuild.lock"
    lock_file.touch(exist_ok=True)
    with open(lock_file, "r+") as lock_f:
        used_lockf = nfs_safe_flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            count = manager.rebuild_from_vectors(
                collection_path=collection_path, lock_already_held=True
            )
        finally:
            nfs_safe_funlock(lock_f.fileno(), used_lockf)

    assert count == 2


@pytest.mark.timeout(10)
def test_rebuild_from_vectors_default_still_works(tmp_path):
    """REGRESSION GUARD: byte-identical default (no lock pre-held)
    behavior (already passes today)."""
    collection_path = _build_real_collection(tmp_path, ["src/a.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")

    count = manager.rebuild_from_vectors(collection_path=collection_path)

    assert count == 1


# --- save_incremental_update() metadata preservation -----------------------


def test_save_incremental_update_preserves_filtered_branch_metadata(tmp_path):
    """Real, verified gap: a filtered rebuild records filtered/current_branch/
    visible_count/total_on_disk in collection_meta.json['hnsw_index'], but
    save_incremental_update() replaces metadata['hnsw_index'] WHOLESALE,
    silently discarding those four fields on the very next incremental
    publish. This must no longer happen.
    """
    collection_path = _build_real_collection(
        tmp_path, ["src/a.py", "src/b.py", "src/c.py"]
    )
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")

    # Perform a real filtered rebuild (branch isolation), matching production.
    manager.rebuild_from_vectors(
        collection_path=collection_path,
        visible_files={"src/a.py", "src/b.py"},
        current_branch="feature-x",
    )

    meta_file = collection_path / "collection_meta.json"
    before = json.loads(meta_file.read_text())["hnsw_index"]
    assert before["filtered"] is True
    assert before["current_branch"] == "feature-x"
    assert before["visible_count"] == 2
    assert before["total_on_disk"] == 3

    # Now perform an incremental update (e.g. one more visible point added)
    # through the SAME manager -- this is the exact call end_indexing()'s
    # incremental path makes.
    index, id_to_label, label_to_id, next_label = manager.load_for_incremental_update(
        collection_path
    )
    assert index is not None

    new_vector = np.array(_vector(99), dtype=np.float32)
    label, id_to_label, label_to_id, next_label = manager.add_or_update_vector(
        index, "p_99", new_vector, id_to_label, label_to_id, next_label
    )

    manager.save_incremental_update(
        index,
        collection_path,
        id_to_label,
        label_to_id,
        vector_count=len(id_to_label),
    )

    after = json.loads(meta_file.read_text())["hnsw_index"]
    assert after["filtered"] is True, "filtered flag must survive incremental publish"
    assert after["current_branch"] == "feature-x", (
        "current_branch must survive incremental publish"
    )
    assert after["visible_count"] == before["visible_count"], (
        "visible_count must be preserved when the caller doesn't explicitly override it"
    )
    assert after["total_on_disk"] == before["total_on_disk"], (
        "total_on_disk must be preserved when the caller doesn't explicitly override it"
    )


def test_save_incremental_update_accepts_explicit_overrides(tmp_path):
    """An explicit override for filtered/current_branch/visible_count/
    total_on_disk must win over whatever was previously stored."""
    collection_path = _build_real_collection(tmp_path, ["src/a.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")

    index, id_to_label, label_to_id, next_label = manager.load_for_incremental_update(
        collection_path
    )
    assert index is not None

    manager.save_incremental_update(
        index,
        collection_path,
        id_to_label,
        label_to_id,
        vector_count=len(id_to_label),
        filtered=True,
        current_branch="main",
        visible_count=1,
        total_on_disk=1,
    )

    meta_file = collection_path / "collection_meta.json"
    after = json.loads(meta_file.read_text())["hnsw_index"]
    assert after["filtered"] is True
    assert after["current_branch"] == "main"
    assert after["visible_count"] == 1
    assert after["total_on_disk"] == 1


def test_save_incremental_update_unfiltered_collection_stays_unfiltered(tmp_path):
    """REGRESSION GUARD: a collection that was NEVER filtered must not
    spontaneously gain a filtered/current_branch/visible_count/total_on_disk
    metadata shape from an incremental update -- byte-identical to today for
    the common, non-branch-isolated case (already passes today)."""
    collection_path = _build_real_collection(tmp_path, ["src/a.py", "src/b.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")

    meta_file = collection_path / "collection_meta.json"
    before = json.loads(meta_file.read_text())["hnsw_index"]
    assert "filtered" not in before or before.get("filtered") in (False, None)

    index, id_to_label, label_to_id, next_label = manager.load_for_incremental_update(
        collection_path
    )
    manager.save_incremental_update(
        index, collection_path, id_to_label, label_to_id, vector_count=len(id_to_label)
    )

    after = json.loads(meta_file.read_text())["hnsw_index"]
    assert after.get("filtered") in (False, None)


# --- validate_hnsw_artifact_for_reuse() ------------------------------------


def test_validate_hnsw_artifact_for_reuse_valid_case(tmp_path):
    collection_path = _build_real_collection(tmp_path, ["src/a.py", "src/b.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")

    ok, reason = manager.validate_hnsw_artifact_for_reuse(
        collection_path, expected_branch=None, expected_filtered=False
    )
    assert ok is True, reason


def test_validate_hnsw_artifact_for_reuse_rejects_stale(tmp_path):
    collection_path = _build_real_collection(tmp_path, ["src/a.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    manager.mark_stale(collection_path)

    ok, reason = manager.validate_hnsw_artifact_for_reuse(
        collection_path, expected_branch=None, expected_filtered=False
    )
    assert ok is False
    assert "stale" in reason.lower()


def test_validate_hnsw_artifact_for_reuse_rejects_missing_index_file(tmp_path):
    collection_path = _build_real_collection(tmp_path, ["src/a.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    (collection_path / manager.INDEX_FILENAME).unlink()

    ok, reason = manager.validate_hnsw_artifact_for_reuse(
        collection_path, expected_branch=None, expected_filtered=False
    )
    assert ok is False


def test_validate_hnsw_artifact_for_reuse_rejects_branch_mismatch(tmp_path):
    collection_path = _build_real_collection(tmp_path, ["src/a.py", "src/b.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    manager.rebuild_from_vectors(
        collection_path=collection_path,
        visible_files={"src/a.py"},
        current_branch="main",
    )

    ok, reason = manager.validate_hnsw_artifact_for_reuse(
        collection_path, expected_branch="other-branch", expected_filtered=True
    )
    assert ok is False
    assert "branch" in reason.lower()


def test_validate_hnsw_artifact_for_reuse_rejects_filtered_flag_mismatch(tmp_path):
    collection_path = _build_real_collection(tmp_path, ["src/a.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")

    ok, reason = manager.validate_hnsw_artifact_for_reuse(
        collection_path, expected_branch=None, expected_filtered=True
    )
    assert ok is False
    assert "filtered" in reason.lower()


def test_validate_hnsw_artifact_for_reuse_rejects_corrupt_index_file(tmp_path):
    collection_path = _build_real_collection(tmp_path, ["src/a.py"])
    manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    index_file = collection_path / manager.INDEX_FILENAME
    index_file.write_bytes(b"not a real hnsw index")

    ok, reason = manager.validate_hnsw_artifact_for_reuse(
        collection_path, expected_branch=None, expected_filtered=False
    )
    assert ok is False
