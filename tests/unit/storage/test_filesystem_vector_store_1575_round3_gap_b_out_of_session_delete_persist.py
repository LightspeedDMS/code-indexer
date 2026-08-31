"""Bug #1575 PathIndex-shortcut mechanism -- THIRD dual-review round, Gap B
(Codex + opus, both independently reproduced; opus found the concrete
real-world trigger): "deletion outside any indexing session is never
persisted."

``delete_points()`` (both the CHUNKS_DB and SHARDED_JSON branches) updates
the in-memory ``self._path_indexes[cache_key]`` correctly when it runs, but
never calls anything that saves it back to ``path_index.bin``, and never
invalidates the on-disk file. ``_save_path_index()`` was, pre-fix, only ever
called from inside ``end_indexing()``. So a delete that happens with NO
active session for that collection
(``collection_name not in self._indexing_session_changes`` -- e.g.
``smart_indexer.py``'s reconcile path and watch-mode deletion-only batch
handling, both of which call ``delete_file_branch_aware()`` ->
``delete_by_filter()`` -> ``delete_points()`` and return BEFORE
``begin_indexing()`` is ever called) left the on-disk ``path_index.bin``
stale across the process boundary.

Reproduced here using TWO GENUINELY SEPARATE ``FilesystemVectorStore``
instances (simulating two separate OS processes, mirroring opus's own
reproduction methodology): instance 1 deletes a file's chunks with no
active session and exits; instance 2 (a fresh store, sharing nothing in
memory with instance 1) starts a session touching a different file. Fix B
requires ``delete_points()`` to persist the update immediately when there is
no active session, so instance 2 must see the correct, post-delete count.

A control test (mirroring the round-2
``test_filesystem_vector_store_1575_regression_missing_path_index.py``
control methodology) proves the underlying delete + full-rescan logic is
already correct on its own: deleting ``path_index.bin`` before instance 2
starts forces the authoritative (non-fast-path) scan regardless of whether
Fix B's persistence landed, isolating that the bug is specifically in the
FAST PATH trusting a stale on-disk bin, not in the deletion logic itself.

Real ``FilesystemVectorStore`` + real filesystem/SQLite throughout -- no
mocking of the code under test.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from _pathindex_gap_1575_helpers import (
    load_measurement_module,
    make_vector,
    read_unique_file_count,
)

TOTAL_FILE_COUNT = 10
EXPECTED_COUNT_AFTER_ONE_FILE_DELETED = TOTAL_FILE_COUNT - 1
TOUCH_VECTOR_SEED = 777


def _build_fixture(tmp_path: Path, *, use_chunks_db: bool, suffix: str):
    mut = load_measurement_module()
    return mut.build_synthetic_fixture(
        tmp_path / f"gap_b_{suffix}",
        num_points=TOTAL_FILE_COUNT,
        chunks_per_file=1,
        use_chunks_db=use_chunks_db,
    )


def _delete_file_with_no_active_session(
    store: FilesystemVectorStore, *, collection_name: str, point_ids
) -> None:
    # Mirrors delete_by_filter()'s real call pattern (smart_indexer.py's
    # reconcile/watch-mode deletion-only paths): delete_points() is called
    # directly, with NO surrounding begin_indexing()/end_indexing() bracket.
    assert collection_name not in store._indexing_session_changes
    delete_result = store.delete_points(collection_name, point_ids)
    assert delete_result["status"] == "ok"


def _touch_a_different_file_in_a_fresh_session(
    base_path: Path,
    *,
    collection_name: str,
    use_chunks_db: bool,
    file_path: str,
) -> None:
    # A genuinely separate store instance -- simulates a second OS process
    # that shares no in-memory state with the instance that performed the
    # out-of-session delete.
    fresh_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=use_chunks_db
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


def _run_cross_process_delete_scenario(
    tmp_path: Path, *, use_chunks_db: bool, suffix: str
) -> int:
    fixture = _build_fixture(tmp_path, use_chunks_db=use_chunks_db, suffix=suffix)
    collection_name = fixture.collection_name
    base_path = fixture.base_path
    deleted_file = fixture.file_paths[0]
    ids_to_delete = fixture.point_ids_by_file[deleted_file]

    # Instance 1 ("process 1"): delete the only chunk of one file, with no
    # active indexing session, then it goes away (nothing further happens
    # on fixture.store).
    _delete_file_with_no_active_session(
        fixture.store, collection_name=collection_name, point_ids=ids_to_delete
    )

    # Instance 2 ("process 2"): a fresh store, touching a DIFFERENT file.
    _touch_a_different_file_in_a_fresh_session(
        base_path,
        collection_name=collection_name,
        use_chunks_db=use_chunks_db,
        file_path=fixture.file_paths[1],
    )

    return int(read_unique_file_count(base_path, collection_name))


def _assert_reflects_the_delete(count: int, *, layout_label: str) -> None:
    assert count == EXPECTED_COUNT_AFTER_ONE_FILE_DELETED, (
        f"expected unique_file_count=={EXPECTED_COUNT_AFTER_ONE_FILE_DELETED} "
        f"({layout_label}) after an out-of-session delete_points() call in a "
        f"separate process removed one file's only chunk -- got {count}. "
        f"delete_points() must persist its in-memory PathIndex update to "
        f"path_index.bin immediately when there is no active indexing "
        f"session for this collection, or the deletion is invisible to the "
        f"next process/session that trusts the stale on-disk bin via the "
        f"fast path."
    )


def test_gap_b_cross_process_delete_sharded_json(tmp_path):
    count = _run_cross_process_delete_scenario(
        tmp_path, use_chunks_db=False, suffix="sharded"
    )
    _assert_reflects_the_delete(count, layout_label="SHARDED_JSON")


def test_gap_b_cross_process_delete_chunks_db(tmp_path):
    count = _run_cross_process_delete_scenario(
        tmp_path, use_chunks_db=True, suffix="chunks_db"
    )
    _assert_reflects_the_delete(count, layout_label="CHUNKS_DB")


def _run_control_scenario(tmp_path: Path, *, use_chunks_db: bool, suffix: str) -> int:
    """Control (mirrors the round-2 missing-path-index control
    methodology): deleting path_index.bin before instance 2 starts forces
    the authoritative disk rescan regardless of whether Fix B's
    out-of-session persistence landed -- proving the underlying delete +
    full-rescan computation is correct on its own, and isolating the bug to
    the fast path specifically."""
    fixture = _build_fixture(
        tmp_path, use_chunks_db=use_chunks_db, suffix=f"{suffix}_ctrl"
    )
    collection_name = fixture.collection_name
    base_path = fixture.base_path
    collection_path = base_path / collection_name
    deleted_file = fixture.file_paths[0]
    ids_to_delete = fixture.point_ids_by_file[deleted_file]

    _delete_file_with_no_active_session(
        fixture.store, collection_name=collection_name, point_ids=ids_to_delete
    )

    # Force the authoritative fallback regardless of Fix B's persistence.
    (collection_path / "path_index.bin").unlink()

    _touch_a_different_file_in_a_fresh_session(
        base_path,
        collection_name=collection_name,
        use_chunks_db=use_chunks_db,
        file_path=fixture.file_paths[1],
    )

    return int(read_unique_file_count(base_path, collection_name))


def test_control_deleting_bin_before_instance2_forces_correct_fallback_sharded_json(
    tmp_path,
):
    count = _run_control_scenario(tmp_path, use_chunks_db=False, suffix="sharded")
    _assert_reflects_the_delete(count, layout_label="SHARDED_JSON control")


def test_control_deleting_bin_before_instance2_forces_correct_fallback_chunks_db(
    tmp_path,
):
    count = _run_control_scenario(tmp_path, use_chunks_db=True, suffix="chunks_db")
    _assert_reflects_the_delete(count, layout_label="CHUNKS_DB control")
