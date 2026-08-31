"""Bug #1575 PathIndex-shortcut mechanism -- the unlocked-save race (project
owner's Fix 3, independently found and reproduced by BOTH Claude opus and
Codex across dual-review rounds).

``_save_path_index()``'s out-of-session persist call sites (``Gap B`` for
deletes, ``Gap D`` for upserts) deliberately call ``PathIndex.save()``
OUTSIDE ``_path_index_lock`` -- required to avoid a lock-ordering deadlock
with ``_id_index_lock`` (the B1 invariant). But ``PathIndex.save()``
iterates the LIVE, still-mutable ``self._path_index`` dict (and each
entry's live, still-mutable ``set`` of point_ids) with NO snapshot at all:

    serializable_data = {
        file_path: list(point_ids)
        for file_path, point_ids in self._path_index.items()
    }

The ``path_index`` object passed to ``_save_path_index`` is frequently the
SAME live object registered in ``self._path_indexes`` -- so a concurrent
``add_point``/``remove_point`` call from another thread (e.g. a second
``upsert_points()``/``delete_points()`` call for the SAME collection,
arriving after the out-of-session persist's caller released
``_path_index_lock``) can mutate the dict/set WHILE this iteration is in
flight, raising ``RuntimeError: dictionary changed size during iteration``.

This test asserts the CORRECT, permanent invariant this fix establishes --
"an unlocked save must never observe a torn read, no matter what a
concurrent mutator does" -- and is EXPECTED to fail with exactly that
``RuntimeError`` against the pre-fix code. It uses REAL threads
(``ThreadPoolExecutor``, no mocking of the code under test) driving the
actual production ``PathIndex.add_point``/``remove_point`` (Thread B,
under the SAME ``_path_index_lock`` real production code always holds
during these mutations) concurrently with the actual production
``FilesystemVectorStore._save_path_index()`` (Thread A, called with NO
lock held -- exactly how the out-of-session persist call sites invoke it).

A ``threading.Barrier`` guarantees both threads start their work at
EXACTLY the same instant. The mutator loops CONTINUOUSLY (bounded only by
a generous safety cap far larger than it could complete before the saver
finishes, per this project's anti-unbounded-loop discipline) until the
saver signals completion via ``stop_event`` -- guaranteeing concurrent
mutation overlaps EVERY save call, not just the first. A LARGE PathIndex
(many thousands of entries) is used so ``PathIndex.save()``'s dict/set
iteration takes long enough in wall-clock terms to give the GIL an
opportunity to interleave with the concurrent mutator. The test also
temporarily lowers ``sys.setswitchinterval()`` (restored in a ``finally``)
so the GIL yields far more often during the race -- at the default
interval (5ms) a single dict-comprehension pass rarely yields mid-loop,
making the race window too narrow to hit reliably; a much shorter interval
makes interleaving practically certain across the many save/mutate
rounds this test runs.

A ``pytest.mark.timeout`` marker (matching Gap C's own methodology)
provides a hard wall-clock backstop for the whole test process.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 8
# Large enough that PathIndex.save()'s dict-comprehension iteration over
# self._path_index.items() (plus one list(point_ids) call per entry) takes
# measurable wall-clock time -- giving concurrent mutation a real chance to
# interleave mid-iteration. Empirically chosen to reproduce the race
# reliably against the pre-fix code without making the test slow.
PREPOPULATED_FILE_COUNT = 8_000
SAVE_ROUNDS = 60
# Generous safety cap (anti-unbounded-loop discipline) -- far larger than
# the mutator could plausibly reach before the saver's SAVE_ROUNDS calls
# finish and set stop_event, so in practice the mutator runs for the
# saver's ENTIRE duration, guaranteeing overlap on every save call rather
# than only the first.
MUTATOR_MAX_ROUNDS = 2_000_000
# Sharply reduced GIL switch interval (default is 0.005s) so the
# interpreter yields far more often during the race, making mid-iteration
# interleaving reliable rather than a rare coincidence.
RACE_SWITCH_INTERVAL_SECONDS = 0.0001
WORKER_TIMEOUT_SECONDS = 30
TEST_TIMEOUT_SECONDS = 90


def _build_store_with_large_live_path_index(tmp_path: Path):
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    collection_name = "coll"
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    store.begin_indexing(collection_name)

    cache_key = store._id_cache_key(collection_name, None)
    path_index = store._path_indexes[cache_key]
    with store._path_index_lock:
        for i in range(PREPOPULATED_FILE_COUNT):
            path_index.add_point(f"src/prepop_{i}.py", f"pt_prepop_{i}")

    return store, collection_name, path_index


def _mutate_path_index_until_stopped(
    store: FilesystemVectorStore,
    path_index,
    barrier: threading.Barrier,
    stop_event: threading.Event,
) -> None:
    """Thread B: real production mutation calls (add_point/remove_point)
    under the SAME lock real upsert_points()/delete_points() code always
    holds during these mutations -- growing AND shrinking the dict (add a
    new key, then remove it entirely) each round, so the dict genuinely
    changes SIZE, not just churns existing values. Runs for the saver's
    ENTIRE duration (bounded by a generous safety cap, never by an
    early completion of its own rounds).
    """
    barrier.wait()
    round_index = 0
    while not stop_event.is_set() and round_index < MUTATOR_MAX_ROUNDS:
        file_path = f"src/mutation_{round_index}.py"
        point_id = f"pt_mutation_{round_index}"
        with store._path_index_lock:
            path_index.add_point(file_path, point_id)
            path_index.remove_point(file_path, point_id)
        round_index += 1


def _save_path_index_repeatedly(
    store: FilesystemVectorStore,
    collection_name: str,
    path_index,
    barrier: threading.Barrier,
    stop_event: threading.Event,
) -> None:
    """Thread A: the actual production out-of-session persist call --
    _save_path_index() invoked with NO lock held, exactly like Gap
    B/Gap D's real call sites.
    """
    barrier.wait()
    try:
        for _ in range(SAVE_ROUNDS):
            store._save_path_index(collection_name, path_index, subdirectory=None)
    finally:
        stop_event.set()


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_unlocked_save_never_raises_dict_changed_size_during_iteration(tmp_path):
    """Permanent regression test: an unlocked _save_path_index() call must
    NEVER raise RuntimeError('dictionary changed size during iteration'),
    even while a concurrent thread is actively add_point()/remove_point()
    mutating the SAME live PathIndex object under _path_index_lock.

    EXPECTED TO FAIL against the pre-fix code with exactly that
    RuntimeError -- PathIndex.save() iterated the live dict/sets with no
    snapshot. Passes once _save_path_index() snapshots the data under
    _path_index_lock before writing.
    """
    store, collection_name, path_index = _build_store_with_large_live_path_index(
        tmp_path
    )
    barrier = threading.Barrier(2)
    stop_event = threading.Event()

    original_switch_interval = sys.getswitchinterval()
    try:
        sys.setswitchinterval(RACE_SWITCH_INTERVAL_SECONDS)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                save_future = executor.submit(
                    _save_path_index_repeatedly,
                    store,
                    collection_name,
                    path_index,
                    barrier,
                    stop_event,
                )
                mutate_future = executor.submit(
                    _mutate_path_index_until_stopped,
                    store,
                    path_index,
                    barrier,
                    stop_event,
                )
                # Future.result() propagates a worker exception (e.g. the
                # RuntimeError this race raises) to this thread, failing
                # the test loudly instead of silently swallowing it.
                save_future.result(timeout=WORKER_TIMEOUT_SECONDS)
                mutate_future.result(timeout=WORKER_TIMEOUT_SECONDS)
        finally:
            stop_event.set()
            store.end_indexing(collection_name)
    finally:
        sys.setswitchinterval(original_switch_interval)
