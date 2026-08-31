"""Bug #1575 PathIndex-shortcut mechanism -- round 7 (opus review round 6,
and opus's follow-up on a later revert, both confirmed real):
``_rebuild_and_repair_path_index()`` SWAPS the live
``self._path_indexes[cache_key]`` entry with a freshly-scanned-from-disk
``PathIndex`` object instead of MERGING into it.

If another thread/process holds a reference to the OLD live object and
adds a point to it "around the same time" this function runs, that
addition is silently discarded when the swap replaces the dict entry --
the persisted ``path_index.bin`` (and the live in-memory picture) then
disagrees with reality. The reviewer's own reproduction: a 25-file
collection where the repair path runs while a concurrent upsert is in
flight ends up with ``path_index.bin`` recording only 1 path, 24 silently
lost.

This is also the confirmed root cause of the flaky
``test_filesystem_vector_store_1575_round3_gap_c_concurrency.py::
test_gap_c_concurrent_delete_and_upsert_never_disagree`` test: once
``end_indexing()`` clears the indexing session, every subsequent
out-of-session ``delete_points()``/``upsert_points()`` call routes through
``_persist_out_of_session_path_index()``, which (because
``_path_index_loaded_from_file`` was never proven True for that cache_key)
falls through to this exact swap path -- so two concurrent out-of-session
calls for the SAME collection can race each other's repair-and-swap.

Mirrors this codebase's own established methodology for this class of bug
(see round 6's Gap D undercount test docstring: "opus reproduced this
SINGLE-PROCESS -- no concurrency needed at all"): the END STATE of the
race (an addition made to the live object right before the repair call
observes/commits its own picture) is reproduced deterministically, rather
than depending on real thread-scheduling timing to land the interleaving.

Real ``FilesystemVectorStore`` + real filesystem/SQLite throughout -- no
mocking of the code under test. Reuses ``build_synthetic_fixture`` (via
``_pathindex_gap_1575_helpers``), the same synthetic-fixture builder every
other round-N test in this file family uses.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from _pathindex_gap_1575_helpers import load_measurement_module

TOTAL_FILE_COUNT = 25
CONCURRENT_FILE_PATH = "src/module_added_concurrently.py"
CONCURRENT_POINT_ID = "pt_added_concurrently"


def _build_fixture(tmp_path: Path, *, use_chunks_db: bool, suffix: str):
    mut = load_measurement_module()
    return mut.build_synthetic_fixture(
        tmp_path / f"round7_{suffix}",
        num_points=TOTAL_FILE_COUNT,
        chunks_per_file=1,
        use_chunks_db=use_chunks_db,
    )


def _run_scenario(tmp_path: Path, *, use_chunks_db: bool, suffix: str):
    fixture = _build_fixture(tmp_path, use_chunks_db=use_chunks_db, suffix=suffix)
    collection_name = fixture.collection_name
    base_path = fixture.base_path

    # A fresh store instance -- mirrors a new process/session picking up
    # the collection. Its own _path_indexes cache starts empty.
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=use_chunks_db
    )
    cache_key = store._id_cache_key(collection_name, None)

    # Populate the live in-memory PathIndex with the current, complete,
    # 25-file picture -- representing the object a concurrent thread/other
    # code path is ALSO holding a reference to.
    live_before = store._load_path_index(collection_name)
    assert len(live_before.all_paths()) == TOTAL_FILE_COUNT
    with store._path_index_lock:
        store._path_indexes[cache_key] = live_before

    # Simulate "another thread ... adds a point to it around the same time
    # this function runs": mutate the SAME object the live dict entry
    # currently points to, BEFORE the repair call executes.
    live_before.add_point(CONCURRENT_FILE_PATH, CONCURRENT_POINT_ID)
    assert CONCURRENT_POINT_ID in live_before.get_point_ids(CONCURRENT_FILE_PATH)

    # The authoritative repair-and-rebuild call -- this is the exact
    # method Bug #1575's SHARDED_JSON _calculate_and_save_unique_file_count
    # branch calls UNCONDITIONALLY on every end_indexing(), and the exact
    # method _persist_out_of_session_path_index() falls back to whenever
    # _path_index_loaded_from_file is not proven True.
    store._rebuild_and_repair_path_index(collection_name, None)

    live_after = store._path_indexes[cache_key]
    return {
        "concurrent_point_ids": live_after.get_point_ids(CONCURRENT_FILE_PATH),
        "all_paths": live_after.all_paths(),
    }


def _assert_concurrent_addition_survived(result, *, layout_label: str) -> None:
    assert CONCURRENT_POINT_ID in result["concurrent_point_ids"], (
        f"[{layout_label}] expected the concurrently-added point "
        f"{CONCURRENT_POINT_ID!r} for {CONCURRENT_FILE_PATH!r} to survive "
        f"_rebuild_and_repair_path_index() -- got point_ids="
        f"{result['concurrent_point_ids']!r}. This is Bug #1575 round 7: "
        f"_rebuild_and_repair_path_index() SWAPS self._path_indexes[cache_key] "
        f"with a freshly-rebuilt-from-disk PathIndex instead of MERGING into "
        f"the live object, silently discarding any concurrent addition made "
        f"to the live object that the disk scan (which never saw this "
        f"never-written-to-disk point) does not know about."
    )
    assert len(result["all_paths"]) == TOTAL_FILE_COUNT + 1, (
        f"[{layout_label}] expected {TOTAL_FILE_COUNT + 1} total distinct "
        f"paths ({TOTAL_FILE_COUNT} original + 1 concurrently added) after "
        f"repair, got {len(result['all_paths'])}: {sorted(result['all_paths'])}"
    )


def test_round7_repair_swap_discards_concurrent_addition_sharded_json(tmp_path):
    result = _run_scenario(tmp_path, use_chunks_db=False, suffix="sharded")
    _assert_concurrent_addition_survived(result, layout_label="SHARDED_JSON")


def test_round7_repair_swap_discards_concurrent_addition_chunks_db(tmp_path):
    result = _run_scenario(tmp_path, use_chunks_db=True, suffix="chunks_db")
    _assert_concurrent_addition_survived(result, layout_label="CHUNKS_DB")
