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
``.metadata.lock``, with NO participation from ``.index_rebuild.lock`` at
all. A concurrent writer holding ONLY ``.index_rebuild.lock`` (another
dirty-before-write, or a full Part C rebuild) therefore provides NO mutual
exclusion against this real-time watch-mode metadata write.

Discriminating design (no mocking of the code under test -- a real,
unmodified ``upsert_points(watch_mode=True)`` call runs end to end; only
pure filesystem observation is used): ``save_incremental_update`` writes
the new HNSW index via a temp file (``.tmp_hnsw_*.tmp`` in the collection
directory) before atomically replacing the real one. That temp file's
on-disk EXISTENCE is an unambiguous, structural marker that execution is
INSIDE ``save_incremental_update`` -- necessarily long after the earlier,
already-correct dirty-before-write released its own lock. The test polls
for that marker and, the INSTANT it appears, checks whether
``.index_rebuild.lock`` is ALSO contended at that exact moment. Pre-fix,
it never is (this call path never touches that lock). Post-fix, the fix
wraps the ENTIRE real-time update -- including this exact save step --
in that lock, so the two conditions are true simultaneously for the whole
window the temp file exists.
"""

import fcntl
import threading
import time

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.utils.file_locking import nfs_safe_flock, nfs_safe_funlock

VECTOR_DIM = 16

_POLL_INTERVAL_SECONDS = 0.001
_JOIN_TIMEOUT_SECONDS = 20
_POLL_MAX_ATTEMPTS = int(_JOIN_TIMEOUT_SECONDS / _POLL_INTERVAL_SECONDS)
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


def _wait_for_hnsw_tmp_file_and_check_lock(
    collection_path, index_rebuild_lock_path, still_running
) -> "tuple[bool, bool]":
    """Polls until a `.tmp_hnsw_*.tmp` file (save_incremental_update's own
    real, unmodified temp-write step) is observed on disk, OR the worker
    finishes, OR the bound is exhausted. Returns
    (tmp_file_seen, lock_contended_at_that_moment).
    """
    attempts = 0
    while still_running() and attempts < _POLL_MAX_ATTEMPTS:
        tmp_files = list(collection_path.glob(".tmp_hnsw_*.tmp"))
        if tmp_files:
            return True, _is_contended_once(index_rebuild_lock_path)
        attempts += 1
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False, False


def _seed_baseline_collection(store):
    store.begin_indexing("coll")
    _ = store.upsert_points(
        "coll",
        [_point(f"seed{i}", f"src/seed{i}.py", i) for i in range(_SEED_POINT_COUNT)],
    )
    _ = store.end_indexing("coll")


@pytest.mark.timeout(30)
def test_watch_mode_realtime_hnsw_update_holds_index_rebuild_lock_during_tmp_save(
    tmp_path,
):
    """The instant save_incremental_update's own real temp-file save step
    is observed on disk (structurally proving execution is inside that
    method, long after the earlier dirty-before-write released its lock),
    .index_rebuild.lock must ALSO be contended -- proving the real-time
    update's metadata write is guarded by the SAME lock every other
    collection_meta.json writer in this file uses.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    _ = store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    _seed_baseline_collection(store)

    lock_file_path = collection_path / ".index_rebuild.lock"
    errors: list = []
    errors_lock = threading.Lock()
    worker_done = threading.Event()

    def _worker():
        try:
            store.upsert_points(
                "coll",
                [_point("watchpoint", "src/watchpoint.py", 999)],
                watch_mode=True,
            )
        except Exception as exc:  # pragma: no cover - failure path
            with errors_lock:
                errors.append(exc)
        finally:
            worker_done.set()

    worker_thread = threading.Thread(target=_worker)
    worker_thread.start()
    tmp_file_seen, lock_contended = _wait_for_hnsw_tmp_file_and_check_lock(
        collection_path, lock_file_path, still_running=lambda: not worker_done.is_set()
    )
    worker_thread.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not worker_thread.is_alive(), "worker thread failed to terminate in time"
    assert not errors, f"watch-mode upsert_points raised: {errors}"
    assert tmp_file_seen, (
        "never observed save_incremental_update's own .tmp_hnsw_*.tmp "
        "temp-write file on disk within the bounded poll -- test setup "
        "issue, not proof of anything about the finding"
    )
    assert lock_contended, (
        ".index_rebuild.lock was NOT contended at the exact moment "
        "save_incremental_update's own temp-file save step was observed "
        "on disk -- the real-time incremental update's metadata write "
        "uses ONLY the independent .metadata.lock, providing NO mutual "
        "exclusion against a concurrent full/incremental Part C rebuild "
        "or another dirty-before-write that both read-merge-write the "
        "same collection_meta.json"
    )
