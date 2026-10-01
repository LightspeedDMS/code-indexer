"""Bug #1575 PathIndex-shortcut mechanism -- round 6, delete-side companion
to ``test_filesystem_vector_store_1575_round6_gap_d_undercount.py``.

Gap B (``delete_points()``'s out-of-session persist) is structurally
identical to Gap D (``upsert_points()``'s out-of-session persist): both
blindly wrote whatever in-memory PathIndex they had to ``path_index.bin``
without checking whether that picture was ever proven complete.

Scenario (single-process, no concurrency needed): build a 25-file
collection through a normal session (``path_index.bin`` correctly has 25
entries), delete the bin (simulating it being lost/never-established),
then have a genuinely FRESH ``FilesystemVectorStore`` instance (no
``begin_indexing()`` call ever made) delete one file's only point via
``delete_points()`` directly -- mirroring ``delete_by_filter()``'s real
out-of-session call pattern (smart_indexer.py's reconcile path, watch-mode
deletion-only handling). Pre-fix, the lazy-loaded PathIndex is EMPTY
(bin missing), so removing a point from it is a no-op, and Gap B then
persists that EMPTY PathIndex to disk -- discarding the other 24 real
files' entries. A THIRD store instance running a normal verification
session then reports ``unique_file_count == 0`` instead of 24, even
though 24 real ``vector_*.json`` files still exist on disk.

Real ``FilesystemVectorStore`` + real filesystem throughout -- no mocking
of the code under test.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)

from _pathindex_gap_1575_helpers import load_measurement_module, read_unique_file_count

TOTAL_FILE_COUNT = 25
EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_DELETE = TOTAL_FILE_COUNT - 1


def _build_fixture_and_delete_bin(tmp_path: Path):
    mut = load_measurement_module()
    fixture = mut.build_synthetic_fixture(
        tmp_path / "gap_b_undercount",
        num_points=TOTAL_FILE_COUNT,
        chunks_per_file=1,
        use_chunks_db=False,
    )
    assert read_unique_file_count(fixture.base_path, fixture.collection_name) == (
        TOTAL_FILE_COUNT
    )

    collection_path = fixture.base_path / fixture.collection_name
    (collection_path / "path_index.bin").unlink()
    return fixture


def _delete_one_file_with_no_active_session(
    base_path: Path, collection_name: str, point_ids
) -> None:
    # A genuinely fresh store instance ("process 2") -- no begin_indexing()
    # ever called for this collection in this instance's lifetime, so
    # _path_index_loaded_from_file has no entry for this cache_key.
    fresh_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    assert collection_name not in fresh_store._indexing_session_changes
    delete_result = fresh_store.delete_points(collection_name, point_ids)
    assert delete_result["status"] == "ok"


def _run_a_normal_verification_session(base_path: Path, collection_name: str) -> None:
    verifying_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    verifying_store.begin_indexing(collection_name)
    verifying_store.end_indexing(collection_name)


def _assert_bin_reflects_out_of_session_delete(
    base_path: Path, collection_name: str, deleted_file: str
) -> None:
    # DISCRIMINATING ASSERTION (retrofit): read path_index.bin's actual
    # persisted content taken at the one point where the round-6 Gap B bug
    # is actually observable -- BEFORE any later session's own authoritative
    # rescan can self-heal path_index.bin regardless of whether Gap B's
    # out-of-session persist itself worked.
    persisted = PathIndex.load(base_path / collection_name / "path_index.bin")
    paths = persisted.all_paths()
    assert len(paths) == EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_DELETE, (
        f"expected path_index.bin to hold "
        f"{EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_DELETE} paths immediately "
        f"after the out-of-session delete, got {len(paths)}: {sorted(paths)} "
        f"-- round-6 catastrophic undercount on the DELETE side: Gap B "
        f"persisted an out-of-session PathIndex that was never proven "
        f"complete."
    )
    assert deleted_file not in paths, (
        f"expected {deleted_file!r} to be ABSENT from path_index.bin right "
        f"after its only point was deleted out-of-session, got "
        f"paths={sorted(paths)}"
    )


def test_gap_b_out_of_session_delete_never_undercounts_when_bin_missing(tmp_path):
    fixture = _build_fixture_and_delete_bin(tmp_path)
    base_path = fixture.base_path
    collection_name = fixture.collection_name
    deleted_file = fixture.file_paths[0]
    ids_to_delete = fixture.point_ids_by_file[deleted_file]

    _delete_one_file_with_no_active_session(base_path, collection_name, ids_to_delete)
    _assert_bin_reflects_out_of_session_delete(base_path, collection_name, deleted_file)

    _run_a_normal_verification_session(base_path, collection_name)

    # Secondary sanity check only (passes unconditionally either way, since
    # the verification session's own end_indexing() self-heals the bin) --
    # still a legitimate end-to-end confirmation of the final state.
    final_count = read_unique_file_count(base_path, collection_name)
    assert final_count == EXPECTED_TOTAL_AFTER_OUT_OF_SESSION_DELETE
