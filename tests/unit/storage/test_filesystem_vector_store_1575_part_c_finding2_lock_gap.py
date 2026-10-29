"""TDD test for Bug #1575 Part C review Finding 2: two independent lock
files guarding writes to the same ``collection_meta.json``.

Confirmed reachable call path (traced via direct code reading, not
guessed): ``upsert_points(..., watch_mode=True)`` calls
``_mark_hnsw_dirty_before_mutation`` FIRST (acquires+releases
``.index_rebuild.lock`` briefly for the dirty-before-write, releasing it
before returning), then SEPARATELY calls
``_update_hnsw_incrementally_realtime`` ->
``HNSWIndexManager.save_incremental_update`` -- which does its OWN
read-merge-write of ``collection_meta.json`` under a DIFFERENT lock file,
``.metadata.lock``. A concurrent writer holding ONLY ``.index_rebuild.lock``
(another dirty-before-write, or a full Part C rebuild) therefore provided
NO mutual exclusion against this real-time watch-mode metadata write until
the fix wrapped the whole real-time update in ``.index_rebuild.lock``.

Discriminating design: a real, unmodified ``upsert_points(watch_mode=True)``
runs end to end. ``save_incremental_update`` publishes the new HNSW index
by ``os.replace`` of its own ``.tmp_hnsw_*.tmp`` temp file; that call is
unambiguously INSIDE the save step, long after the dirty-before-write
released its lock. An observation spy around ``os.replace`` (it calls the
real ``os.replace`` afterwards) checks, at that exact instant, whether
``.index_rebuild.lock`` is held. The check runs synchronously in the
saving thread, so it never races the save -- the earlier version polled
for the temp file from a second thread and lost the race on tmpfs, where
the whole save finishes between two polls.
"""

import fcntl
import os
from pathlib import Path

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock

VECTOR_DIM = 16

_SEED_POINT_COUNT = 80


def _vector(seed: int):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


def _point(point_id, path, seed):
    return {
        "id": point_id,
        "vector": _vector(seed),
        "payload": {"path": path, "type": "content", "hidden_branches": []},
    }


def _is_contended_once(lock_file_path) -> bool:
    """Non-blocking trylock probe: True iff the lock is currently held by
    someone else (acquisition fails with OSError)."""
    lock_file_path.touch(exist_ok=True)
    with open(lock_file_path, "r+") as probe_fd:
        try:
            used_lockf = nfs_safe_flock(
                probe_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except OSError:
            return True
        nfs_safe_funlock(probe_fd.fileno(), used_lockf)
        return False


def _seed_baseline_collection(store):
    store.begin_indexing("coll")
    _ = store.upsert_points(
        "coll",
        [_point(f"seed{i}", f"src/seed{i}.py", i) for i in range(_SEED_POINT_COUNT)],
    )
    _ = store.end_indexing("coll")


@pytest.mark.timeout(30)
def test_watch_mode_realtime_hnsw_update_holds_index_rebuild_lock_during_tmp_save(
    tmp_path, monkeypatch
):
    """At the instant save_incremental_update publishes its own temp-file
    save (os.replace of .tmp_hnsw_*.tmp), .index_rebuild.lock must be held
    -- the real-time update's metadata write is guarded by the SAME lock
    every other collection_meta.json writer in this file uses.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    _seed_baseline_collection(store)
    lock_file_path = collection_path / ".index_rebuild.lock"

    real_replace = os.replace
    contended_at_publish: list = []

    def _observe_publish(src, dst, *args, **kwargs):
        if Path(os.fspath(src)).name.startswith(".tmp_hnsw_"):
            contended_at_publish.append(_is_contended_once(lock_file_path))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _observe_publish)
    store.upsert_points(
        "coll",
        [_point("watchpoint", "src/watchpoint.py", 999)],
        watch_mode=True,
    )
    monkeypatch.setattr(os, "replace", real_replace)

    assert contended_at_publish, (
        "the watch-mode upsert never published a .tmp_hnsw_*.tmp file -- "
        "save_incremental_update did not run, so nothing was proven"
    )
    assert all(contended_at_publish), (
        ".index_rebuild.lock was NOT held when save_incremental_update "
        "published its temp-file save -- the real-time incremental update's "
        "metadata write uses ONLY the independent .metadata.lock, providing NO "
        "mutual exclusion against a concurrent full/incremental Part C rebuild "
        "or another dirty-before-write that both read-merge-write the same "
        "collection_meta.json"
    )
