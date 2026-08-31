"""Bug #1575 PathIndex-shortcut mechanism -- round 6 (Codex + opus dual
review of round 5's diff). CRITICAL finding: Gap D's (and the structurally
identical, pre-existing Gap B's) out-of-session persist blindly writes
whatever in-memory PathIndex it currently has to path_index.bin, without
ever checking whether that picture was actually PROVEN complete --
reintroducing the round-2 catastrophic-undercount bug through a new door.

``begin_indexing()`` only checks the bin's mere EXISTENCE as a "proven
complete" signal (``self._path_index_loaded_from_file[cache_key] =
path_index_bin.exists()``); it does not check whether the in-memory
PathIndex used for the Gap D/B out-of-session persist was actually
loaded-from-file vs freshly-created-empty (or partially populated by only
this call's own out-of-session mutation).

opus reproduced this SINGLE-PROCESS (no concurrency needed at all): build
a 25-file collection through a normal begin_indexing()/upsert_points()/
end_indexing() session (path_index.bin correctly ends up with 25
entries), delete path_index.bin (simulating it being lost/never
established for some other reason -- e.g. a fresh process whose very
first touch to this collection is an out-of-session upsert), then have a
genuinely FRESH ``FilesystemVectorStore`` instance (no ``begin_indexing()``
call ever made against it -- mirrors watch mode or any other
out-of-session ``upsert_points()`` caller) upsert exactly ONE new file.
Pre-fix, the out-of-session Gap D persist writes path_index.bin with only
that ONE file's entry -- silently discarding the other 25 real,
still-on-disk files from the recorded picture. A THIRD store instance
then runs an ordinary session (``begin_indexing()`` sees the bin exists
-> trusts it; ``end_indexing()`` computes ``unique_file_count`` from the
now-corrupted, trusted PathIndex) and reports ``unique_file_count == 1``
instead of 26 (25 original + 1 new), even though 26 real
``vector_*.json`` files still exist on disk at that moment.

The fix gates Gap D's (and Gap B's) out-of-session persist on
``self._path_index_loaded_from_file[cache_key]`` being True (i.e. the
in-memory picture was actually loaded from an existing, presumed-complete
path_index.bin, or previously repaired) -- when it is not, the persist
path forces ``_rebuild_and_repair_path_index()`` (an authoritative
``rglob`` disk rescan) instead of trusting the partial live picture, so
the persisted bin can never regress below the true on-disk state.

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

TOTAL_FILE_COUNT = 25
NEW_FILE_PATH = "src/module_new_out_of_session.py"
EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_UPSERT = TOTAL_FILE_COUNT + 1


def _build_fixture_and_delete_bin(tmp_path: Path):
    mut = load_measurement_module()
    fixture = mut.build_synthetic_fixture(
        tmp_path / "gap_d_undercount",
        num_points=TOTAL_FILE_COUNT,
        chunks_per_file=1,
        use_chunks_db=False,
    )
    # Sanity: the normal session correctly recorded all 25 files.
    assert read_unique_file_count(fixture.base_path, fixture.collection_name) == (
        TOTAL_FILE_COUNT
    )

    # Simulate the bin being lost/never-established (round 6 opus repro):
    # deleting it forces the NEXT lazy-load to see "no bin", exactly
    # mirroring the scenario where begin_indexing() was never called for
    # this collection in the process performing the out-of-session upsert.
    collection_path = fixture.base_path / fixture.collection_name
    (collection_path / "path_index.bin").unlink()
    return fixture


def _upsert_one_new_file_with_no_active_session(
    base_path: Path, collection_name: str
) -> None:
    # A genuinely fresh store instance ("process 2") -- no begin_indexing()
    # ever called for this collection in this instance's lifetime, so
    # _path_index_loaded_from_file has no entry for this cache_key.
    fresh_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    assert collection_name not in fresh_store._indexing_session_changes
    upsert_result = fresh_store.upsert_points(
        collection_name,
        [
            {
                "id": "pt_new_out_of_session",
                "vector": make_vector(999),
                "payload": {
                    "path": NEW_FILE_PATH,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    assert upsert_result["status"] == "ok"


def _run_a_normal_verification_session(base_path: Path, collection_name: str) -> None:
    # A THIRD store instance ("process 3") runs a normal session with NO
    # new upserts of its own, just to surface whatever picture
    # path_index.bin now holds via the normal begin_indexing()/
    # end_indexing() trust path.
    verifying_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    verifying_store.begin_indexing(collection_name)
    verifying_store.end_indexing(collection_name)


def _assert_bin_reflects_out_of_session_upsert(
    base_path: Path, collection_name: str
) -> None:
    # DISCRIMINATING ASSERTION (retrofit): read path_index.bin's actual
    # persisted content taken at the one point where the round-6 bug is
    # actually observable -- BEFORE any later session's own authoritative
    # rescan (_calculate_and_save_unique_file_count()'s SHARDED_JSON
    # project-owner FINAL decision) can self-heal path_index.bin regardless
    # of whether Gap D's out-of-session persist itself worked.
    persisted = PathIndex.load(base_path / collection_name / "path_index.bin")
    paths = persisted.all_paths()
    assert len(paths) == EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_UPSERT, (
        f"expected path_index.bin to hold "
        f"{EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_UPSERT} paths immediately "
        f"after the out-of-session upsert, got {len(paths)}: {sorted(paths)} "
        f"-- round-6 catastrophic undercount: Gap D persisted an "
        f"out-of-session PathIndex that was never proven complete."
    )
    assert NEW_FILE_PATH in paths, (
        f"expected {NEW_FILE_PATH!r} in path_index.bin right after the "
        f"out-of-session upsert that added it, got paths={sorted(paths)}"
    )


def test_gap_d_out_of_session_upsert_never_undercounts_when_bin_missing(tmp_path):
    fixture = _build_fixture_and_delete_bin(tmp_path)
    base_path = fixture.base_path
    collection_name = fixture.collection_name

    _upsert_one_new_file_with_no_active_session(base_path, collection_name)
    _assert_bin_reflects_out_of_session_upsert(base_path, collection_name)

    _run_a_normal_verification_session(base_path, collection_name)

    # Secondary sanity check only (passes unconditionally either way, since
    # the verification session's own end_indexing() self-heals the bin) --
    # still a legitimate end-to-end confirmation of the final state.
    final_count = read_unique_file_count(base_path, collection_name)
    assert final_count == EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_UPSERT
