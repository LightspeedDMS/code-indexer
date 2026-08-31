"""Bug #1575 PathIndex-shortcut mechanism -- Gap D (project owner's Fix 2,
mirroring the already-fixed Gap B for deletes): "insertion outside any
indexing session is never persisted" -- SHARDED_JSON only (per the project
owner's scoping decision: CHUNKS_DB's fast-path shortcut for
unique_file_count was reverted entirely in Fix 1, so it has no equivalent
regression for that specific symptom; this fix targets SHARDED_JSON's
kept-and-fixed shortcut mechanism).

``upsert_points()``'s SHARDED_JSON branch updates the in-memory
``self._path_indexes[cache_key]`` correctly (``path_index.add_point(...)``
inside the per-point loop) when it runs, but never calls anything that
saves it back to ``path_index.bin`` when there is NO active indexing
session for this collection (``collection_name not in
self._indexing_session_changes`` -- e.g. watch mode, or any other
out-of-session ``upsert_points()`` call). ``_save_path_index()`` was, pre-
fix, only ever called from inside ``end_indexing()`` or from
``delete_points()``'s own Round 3 Fix B persist. So an out-of-session
upsert that adds a NEW file leaves the on-disk ``path_index.bin`` stale
across a process boundary -- a LATER session (a fresh process) that trusts
the fast path via ``_get_live_session_path_index`` undercounts by the
newly-added file(s).

Reproduced here using TWO GENUINELY SEPARATE ``FilesystemVectorStore``
instances (simulating two separate OS processes, mirroring Gap B's own
methodology): instance 1 upserts an 11th file with NO active session and
exits; instance 2 (a fresh store, sharing nothing in memory with instance
1) starts a session touching a different, already-existing file. Fix D
requires ``upsert_points()`` to persist the update immediately when there
is no active session, so instance 2 must see the correct, post-upsert
count (11), not the stale pre-upsert count (10).

A control test (mirroring Gap B's own control methodology) proves the
underlying upsert + full-rescan logic is already correct on its own:
deleting ``path_index.bin`` before instance 2 starts forces the
authoritative (non-fast-path) scan regardless of whether Fix D's
persistence landed, isolating that the bug is specifically in the FAST
PATH trusting a stale on-disk bin, not in the upsert logic itself.

Real ``FilesystemVectorStore`` + real filesystem throughout -- no mocking
of the code under test.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)

from _pathindex_gap_1575_helpers import (
    load_measurement_module,
    make_vector,
    read_unique_file_count,
)

TOTAL_FILE_COUNT = 10
EXPECTED_COUNT_AFTER_ONE_FILE_ADDED = TOTAL_FILE_COUNT + 1
NEW_FILE_PATH = "src/module_new_out_of_session.py"
NEW_FILE_VECTOR_SEED = 555
TOUCH_VECTOR_SEED = 777


def _build_fixture(tmp_path: Path, *, suffix: str):
    mut = load_measurement_module()
    return mut.build_synthetic_fixture(
        tmp_path / f"gap_d_{suffix}",
        num_points=TOTAL_FILE_COUNT,
        chunks_per_file=1,
        use_chunks_db=False,
    )


def _upsert_new_file_with_no_active_session(
    store: FilesystemVectorStore, *, collection_name: str
) -> None:
    # Mirrors a real out-of-session call pattern (watch mode's
    # upsert_points() calls, and any other caller that upserts without a
    # surrounding begin_indexing()/end_indexing() bracket): upsert_points()
    # is called directly, with no active session.
    assert collection_name not in store._indexing_session_changes
    upsert_result = store.upsert_points(
        collection_name,
        [
            {
                "id": "pt_new_out_of_session",
                "vector": make_vector(NEW_FILE_VECTOR_SEED),
                "payload": {
                    "path": NEW_FILE_PATH,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    assert upsert_result["status"] == "ok"


def _touch_a_different_file_in_a_fresh_session(
    base_path: Path, *, collection_name: str, file_path: str
) -> None:
    # A genuinely separate store instance -- simulates a second OS process
    # that shares no in-memory state with the instance that performed the
    # out-of-session upsert.
    fresh_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    fresh_store.begin_indexing(collection_name)
    upsert_result = fresh_store.upsert_points(
        collection_name,
        [
            {
                "id": "pt_touch_instance2",
                "vector": make_vector(TOUCH_VECTOR_SEED),
                "payload": {
                    "path": file_path,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    assert upsert_result["status"] == "ok"
    fresh_store.end_indexing(collection_name)


def _assert_bin_reflects_out_of_session_upsert(
    base_path: Path, collection_name: str
) -> None:
    # DISCRIMINATING ASSERTION (retrofit): read path_index.bin's actual
    # persisted content taken at the one point where Gap D's persist-time
    # correctness is actually observable -- BEFORE instance 2's own full
    # session (_touch_a_different_file_in_a_fresh_session) runs its own
    # end_indexing(), which ALWAYS forces a fresh authoritative rescan that
    # would self-heal path_index.bin regardless of whether Gap D's
    # out-of-session persist itself worked.
    persisted = PathIndex.load(base_path / collection_name / "path_index.bin")
    paths = persisted.all_paths()
    assert NEW_FILE_PATH in paths, (
        f"expected {NEW_FILE_PATH!r} to be present in path_index.bin "
        f"immediately after instance 1's out-of-session upsert (before "
        f"instance 2's own session can self-heal the bin), got "
        f"paths={sorted(paths)} -- Gap D's out-of-session persist did not "
        f"land."
    )
    assert len(paths) == EXPECTED_COUNT_AFTER_ONE_FILE_ADDED, (
        f"expected path_index.bin to hold "
        f"{EXPECTED_COUNT_AFTER_ONE_FILE_ADDED} paths immediately after "
        f"instance 1's out-of-session upsert, got {len(paths)}: "
        f"{sorted(paths)}"
    )


def _run_cross_process_upsert_scenario(tmp_path: Path, *, suffix: str) -> int:
    fixture = _build_fixture(tmp_path, suffix=suffix)
    collection_name = fixture.collection_name
    base_path = fixture.base_path

    # Instance 1 ("process 1"): upsert a brand-new 11th file, with no
    # active indexing session, then it goes away (nothing further happens
    # on fixture.store).
    _upsert_new_file_with_no_active_session(
        fixture.store, collection_name=collection_name
    )
    _assert_bin_reflects_out_of_session_upsert(base_path, collection_name)

    # Instance 2 ("process 2"): a fresh store, touching a DIFFERENT
    # (already-existing) file.
    _touch_a_different_file_in_a_fresh_session(
        base_path,
        collection_name=collection_name,
        file_path=fixture.file_paths[1],
    )

    return int(read_unique_file_count(base_path, collection_name))


def _assert_reflects_the_upsert(count: int) -> None:
    assert count == EXPECTED_COUNT_AFTER_ONE_FILE_ADDED, (
        f"expected unique_file_count=={EXPECTED_COUNT_AFTER_ONE_FILE_ADDED} "
        f"(SHARDED_JSON) after an out-of-session upsert_points() call in a "
        f"separate process added one new file -- got {count}. "
        f"upsert_points() must persist its in-memory PathIndex update to "
        f"path_index.bin immediately when there is no active indexing "
        f"session for this collection, or the addition is invisible to "
        f"the next process/session that trusts the stale on-disk bin via "
        f"the fast path."
    )


def test_gap_d_cross_process_upsert_sharded_json(tmp_path):
    count = _run_cross_process_upsert_scenario(tmp_path, suffix="sharded")
    _assert_reflects_the_upsert(count)


def _run_control_scenario(tmp_path: Path, *, suffix: str) -> int:
    """Control (mirrors Gap B's own control methodology): deleting
    path_index.bin before instance 2 starts forces the authoritative disk
    rescan regardless of whether Fix D's out-of-session persistence
    landed -- proving the underlying upsert + full-rescan computation is
    correct on its own, and isolating the bug to the fast path
    specifically."""
    fixture = _build_fixture(tmp_path, suffix=f"{suffix}_ctrl")
    collection_name = fixture.collection_name
    base_path = fixture.base_path
    collection_path = base_path / collection_name

    _upsert_new_file_with_no_active_session(
        fixture.store, collection_name=collection_name
    )

    # Force the authoritative fallback regardless of Fix D's persistence.
    (collection_path / "path_index.bin").unlink()

    _touch_a_different_file_in_a_fresh_session(
        base_path,
        collection_name=collection_name,
        file_path=fixture.file_paths[1],
    )

    return int(read_unique_file_count(base_path, collection_name))


def test_control_deleting_bin_before_instance2_forces_correct_fallback_sharded_json(
    tmp_path,
):
    count = _run_control_scenario(tmp_path, suffix="sharded")
    _assert_reflects_the_upsert(count)
