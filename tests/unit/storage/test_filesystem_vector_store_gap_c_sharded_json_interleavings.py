"""SHARDED_JSON half of the Gap C delete/upsert race: the contended point's
vector file on disk and the live PathIndex entry must agree after a
concurrent delete_points()/upsert_points() of the same point.

Real store, real threads. One of the store's own locks is wrapped so one
worker pauses (bounded wait) right after it changes the contended point's
state; the other worker runs in exactly that gap.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from _pathindex_gap_1575_helpers import make_vector

POINT_ID = "contended_point"
OTHER_POINT_ID = "other_point"
FILE_PATH = "src/contended.py"
COPY_PATH = "src/copy.py"
COLLECTION = "coll"
VECTOR_SIZE = 8
# ^ why bounded pause, not an unbounded wait; a correct fix blocks the other worker until the paused one resumes
PAUSE_SECONDS = 1.0
WORKER_TIMEOUT_SECONDS = 30
TEST_TIMEOUT_SECONDS = 60

GateChange = Callable[[Any, Any], bool]


# ^ invariant gated thread pauses once, after release, <= PAUSE_SECONDS, only on a matching read_state() change
class _PauseAfterChange:
    def __init__(
        self,
        inner: Any,
        read_state: Callable[[], Any],
        is_gate_change: GateChange,
    ):
        self._inner = inner
        self._read_state = read_state
        self._is_gate_change = is_gate_change
        self._before: Any = None
        self.gated_ident: Optional[int] = None
        self.paused = threading.Event()
        self.other_done = threading.Event()

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self._inner.acquire(*args, **kwargs))

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> "_PauseAfterChange":
        self._inner.acquire()
        self._before = self._read_state()
        return self

    def __exit__(self, *exc: Any) -> None:
        before, after = self._before, self._read_state()
        self._inner.release()
        if (
            threading.get_ident() == self.gated_ident
            and not self.paused.is_set()
            and self._is_gate_change(before, after)
        ):
            self.paused.set()
            self.other_done.wait(PAUSE_SECONDS)


def _id_index_entry(store: FilesystemVectorStore) -> Callable[[], Optional[Path]]:
    return lambda: store._id_index.get(COLLECTION, {}).get(POINT_ID)


def _id_index_gate(
    store: FilesystemVectorStore, is_gate_change: GateChange
) -> _PauseAfterChange:
    return _PauseAfterChange(
        store._id_index_lock, _id_index_entry(store), is_gate_change
    )


def _point(seed: int, point_id: str = POINT_ID, path: str = FILE_PATH):
    payload = {"path": path, "type": "content", "hidden_branches": []}
    return {"id": point_id, "vector": make_vector(seed), "payload": payload}


def _sharded_json_store(tmp_path: Path, point_present: bool) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_SIZE)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, [_point(seed=1)])
    if not point_present:
        store.delete_points(COLLECTION, [POINT_ID])
    store.end_indexing(COLLECTION)
    return store


def _run(
    store: FilesystemVectorStore,
    lock_attribute: str,
    gate: _PauseAfterChange,
    gated: Callable[[], object],
    other: Callable[[], object],
) -> None:
    def run_gated() -> None:
        gate.gated_ident = threading.get_ident()
        gated()

    def run_other() -> None:
        assert gate.paused.wait(WORKER_TIMEOUT_SECONDS), "gated worker never paused"
        try:
            other()
        finally:
            gate.other_done.set()

    setattr(store, lock_attribute, gate)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_gated), executor.submit(run_other)]
        for future in futures:
            future.result(timeout=WORKER_TIMEOUT_SECONDS)


def _file_has_point(tmp_path: Path) -> bool:
    return any((tmp_path / COLLECTION).rglob(f"vector_{POINT_ID}.json"))


def _cache_has_point(store: FilesystemVectorStore, path: str = FILE_PATH) -> bool:
    return POINT_ID in store._path_indexes[COLLECTION].get_point_ids(path)


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_delete_between_upsert_index_write_and_path_index_add(tmp_path):
    store = _sharded_json_store(tmp_path, point_present=False)
    gate = _id_index_gate(store, lambda b, a: b is None and a is not None)

    _run(
        store,
        "_id_index_lock",
        gate,
        gated=lambda: store.upsert_points(COLLECTION, [_point(seed=2)]),
        other=lambda: store.delete_points(COLLECTION, [POINT_ID]),
    )

    assert gate.paused.is_set()
    assert _file_has_point(tmp_path) == _cache_has_point(store), (
        f"vector file present={_file_has_point(tmp_path)} but PathIndex "
        f"present={_cache_has_point(store)}"
    )


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_upsert_between_delete_index_removal_and_path_index_removal(tmp_path):
    store = _sharded_json_store(tmp_path, point_present=True)
    gate = _id_index_gate(store, lambda b, a: b is not None and a is None)

    _run(
        store,
        "_id_index_lock",
        gate,
        gated=lambda: store.delete_points(COLLECTION, [POINT_ID]),
        other=lambda: store.upsert_points(COLLECTION, [_point(seed=2)]),
    )

    assert gate.paused.is_set()
    assert _file_has_point(tmp_path) == _cache_has_point(store), (
        f"vector file present={_file_has_point(tmp_path)} but PathIndex "
        f"present={_cache_has_point(store)}"
    )


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_orphan_cleanup_never_destroys_a_copy_upserted_in_the_gap(tmp_path):
    store = _sharded_json_store(tmp_path, point_present=True)
    path_index = store._path_indexes[COLLECTION]
    gate = _PauseAfterChange(
        store._path_index_lock,
        lambda: POINT_ID in path_index.get_point_ids(FILE_PATH),
        lambda before, after: before and not after,
    )

    _run(
        store,
        "_path_index_lock",
        gate,
        gated=lambda: store.upsert_points(
            COLLECTION, [_point(seed=3, point_id=OTHER_POINT_ID)]
        ),
        other=lambda: store.upsert_points(COLLECTION, [_point(seed=1, path=COPY_PATH)]),
    )

    assert gate.paused.is_set()
    assert _file_has_point(tmp_path) == _cache_has_point(store, COPY_PATH), (
        f"vector file present={_file_has_point(tmp_path)} but PathIndex "
        f"present under the copy={_cache_has_point(store, COPY_PATH)}"
    )
