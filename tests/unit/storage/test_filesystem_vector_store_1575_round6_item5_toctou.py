"""Bug #1575 round 6, item 5 (Codex TOCTOU concern, opus's counter-
assessment): Codex flagged that Gap D's "no active session" check
(``collection_name not in self._indexing_session_changes``) is not atomic
with the actual persist -- a concurrent ``begin_indexing()`` call for the
SAME collection could interleave between the check and the snapshot+save.
Opus's assessment is that Gap D's real defect was item 1 (persisting an
unproven/partial picture), and that this TOCTOU may become MOOT once item
1's provenance-gating fix lands (since the false branch now forces an
authoritative disk rescan via ``_rebuild_and_repair_path_index()``, which
reads the TRUE on-disk state regardless of in-memory races).

Bug #1823 (truthfulness correction): this test was originally built to
verify empirically whether the TOCTOU race causes real data loss, using
real thread scheduling to try to interleave an out-of-session
``upsert_points()`` (Gap D's own path) against a concurrent
``begin_indexing()``/``upsert_points()``/``end_indexing()`` session. It
CANNOT actually detect that race, at any trial count: an isolated
verification script runtime-monkeypatched
``_persist_out_of_session_path_index`` to insert a 0.05s sleep between the
lock-protected capture of the live PathIndex and the ``_save_path_index()``
call -- deliberately forcing the TOCTOU window wide open, far beyond what
real thread scheduling could ever produce naturally -- then delegated to
the REAL, unmodified ``_save_path_index``/``_rebuild_and_repair_path_index``
(production source on disk verified byte-identical, md5sum, throughout).
10 trials against that deliberately-forced-open window produced ZERO data
loss. This is consistent with the module docstring's "may become moot"
assessment above: item 1's provenance-gating fix re-reads
``self._path_indexes[cache_key]`` live, under lock, immediately before any
window could open, so the captured reference already reflects the
freshest write ordering available at read time -- there may be no real
race left for ANY trial count or window width to catch on the current
production code shape.

Given that, this test is kept as a cheap SMOKE CHECK ONLY: real threads,
a single SHARED ``FilesystemVectorStore`` instance, real concurrent
out-of-session-upsert-vs-session-upsert load, asserting the property that
actually matters end-to-end (a fresh, independent verification store's
``unique_file_count`` matches the true total of distinct files written).
It is NOT relied upon to catch a regression in the TOCTOU window itself --
see ``test_gap_d_toctou_race_smoke_check_no_data_loss``'s own docstring.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from _pathindex_gap_1575_helpers import make_vector, read_unique_file_count

# Bug #1823: lowered from 40, then from 10. See the module docstring's
# "truthfulness correction" paragraph for the full investigation: this
# test cannot detect the TOCTOU race it was originally built to catch, at
# ANY trial count -- even a 0.05s forced-open window produced zero data
# loss across 10 trials. With no evidence this test can ever go RED for
# its intended purpose, a large trial count buys nothing; 3 trials is
# enough to keep it a meaningful smoke check (real concurrent load,
# multiple distinct files) while keeping wall time low.
NUM_TRIALS = 3
WORKER_COUNT = 2
WORKER_TIMEOUT_SECONDS = 30
TEST_TIMEOUT_SECONDS = 90
VECTOR_SIZE = 8
COLLECTION_NAME = "coll"
BASELINE_FILE = "src/baseline.py"


def _build_baseline_store(tmp_path: Path) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(COLLECTION_NAME, vector_size=VECTOR_SIZE)
    store.begin_indexing(COLLECTION_NAME)
    store.upsert_points(
        COLLECTION_NAME,
        [
            {
                "id": "pt_baseline",
                "vector": make_vector(0),
                "payload": {
                    "path": BASELINE_FILE,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    store.end_indexing(COLLECTION_NAME)
    return store


def _run_one_race_trial(
    store: FilesystemVectorStore, trial: int, barrier: threading.Barrier
) -> None:
    out_of_session_path = f"src/out_of_session_{trial}.py"
    in_session_path = f"src/in_session_{trial}.py"

    def do_out_of_session_upsert() -> None:
        barrier.wait()
        store.upsert_points(
            COLLECTION_NAME,
            [
                {
                    "id": f"pt_oos_{trial}",
                    "vector": make_vector(1000 + trial),
                    "payload": {
                        "path": out_of_session_path,
                        "type": "content",
                        "hidden_branches": [],
                    },
                }
            ],
        )

    def do_in_session_upsert() -> None:
        barrier.wait()
        store.begin_indexing(COLLECTION_NAME)
        store.upsert_points(
            COLLECTION_NAME,
            [
                {
                    "id": f"pt_ins_{trial}",
                    "vector": make_vector(2000 + trial),
                    "payload": {
                        "path": in_session_path,
                        "type": "content",
                        "hidden_branches": [],
                    },
                }
            ],
        )
        store.end_indexing(COLLECTION_NAME)

    with ThreadPoolExecutor(max_workers=WORKER_COUNT) as executor:
        oos_future = executor.submit(do_out_of_session_upsert)
        ins_future = executor.submit(do_in_session_upsert)
        oos_future.result(timeout=WORKER_TIMEOUT_SECONDS)
        ins_future.result(timeout=WORKER_TIMEOUT_SECONDS)


@pytest.mark.timeout(TEST_TIMEOUT_SECONDS)
def test_gap_d_toctou_race_smoke_check_no_data_loss(tmp_path):
    """Bug #1823: SMOKE CHECK ONLY -- see the module docstring's
    "truthfulness correction" paragraph. This test does NOT reliably
    detect the Codex item-5 TOCTOU race it was originally written to
    catch: a separate investigation forced the race window open by
    50x normal (a 0.05s monkeypatched sleep, vs. real thread-scheduling
    gaps of microseconds) and still saw zero data loss across 10 trials
    against the real, unmodified production code. What remains here is
    real concurrent load (real threads, a shared store instance, an
    out-of-session upsert racing a normal indexing session) asserting
    that no data is lost end-to-end -- a property worth re-validating
    against future refactors of the persist path, even though a failure
    here is more likely to indicate an unrelated regression in that path
    than a reproduction of the original TOCTOU concern.
    """
    store = _build_baseline_store(tmp_path)
    barrier = threading.Barrier(WORKER_COUNT)

    for trial in range(NUM_TRIALS):
        barrier.reset()
        _run_one_race_trial(store, trial, barrier)

    # Independent verification: a FRESH, uninvolved store instance (mirrors
    # the round-3 Gap B/D "separate process" simulation) runs a no-op
    # session just to surface whatever path_index.bin now holds.
    verifying_store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    verifying_store.begin_indexing(COLLECTION_NAME)
    verifying_store.end_indexing(COLLECTION_NAME)

    expected_total = 1 + (NUM_TRIALS * 2)  # baseline + 2 new files per trial
    final_count = read_unique_file_count(tmp_path, COLLECTION_NAME)
    assert final_count == expected_total, (
        f"expected unique_file_count == {expected_total} (1 baseline file "
        f"+ 2 distinct new files per trial across {NUM_TRIALS} racing "
        f"trials of an out-of-session upsert vs. a concurrent "
        f"begin_indexing()/end_indexing() session), got {final_count} -- "
        f"this smoke check is not known to detect the original Codex "
        f"item-5 TOCTOU concern (see module docstring), so a failure here "
        f"more likely indicates an unrelated regression in the "
        f"out-of-session persist path than a reproduction of that race."
    )
