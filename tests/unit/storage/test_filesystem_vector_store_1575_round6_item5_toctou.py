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

This test verifies empirically rather than assuming either way: real
threads, a single SHARED ``FilesystemVectorStore`` instance (so the race
is on the SAME in-memory ``_indexing_session_changes``/``_path_indexes``
state a genuine concurrent ``begin_indexing()`` would contend on), driving
many trials of an out-of-session ``upsert_points()`` call (Gap D's own
path) racing against a normal ``begin_indexing()``/``upsert_points()``/
``end_indexing()`` session for the SAME collection -- each trial adding
TWO distinct new files (one per thread). Real thread scheduling means the
exact interleave point cannot be pinned deterministically (asserting on it
would be scheduling-dependent and flaky), so this test instead asserts on
the property that actually matters: after all trials, an independent
verification pass (a fresh, uninvolved store instance, mirroring the
"separate process" simulation the round-3 Gap B/D tests use) confirms the
final ``unique_file_count`` equals the TRUE total of distinct files
written, proving no data was lost across the repeated race.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from _pathindex_gap_1575_helpers import make_vector, read_unique_file_count

NUM_TRIALS = 40
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
def test_gap_d_toctou_race_never_loses_data_across_many_trials(tmp_path):
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
        f"this would indicate the Gap D TOCTOU race (Codex's item 5 "
        f"concern) causes real data loss even after item 1's "
        f"provenance-gating fix."
    )
