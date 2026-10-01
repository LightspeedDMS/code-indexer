"""Bug #1575 PathIndex-shortcut mechanism -- round 6, item 4 (Codex claim,
independently investigated and CONFIRMED real by this session).

Codex flagged that CHUNKS_DB may lack out-of-session PathIndex persistence
for orphan/duplicate-cleanup consumers, separate from the already-fixed
``unique_file_count`` fast path (which, for CHUNKS_DB, always queries
``chunk_store.distinct_paths()`` directly and never trusts PathIndex at
all -- so this defect is invisible to a unique_file_count assertion).

Investigation confirmed the gap: ``_upsert_points_chunks_db()`` lazily
loads ``path_index.bin`` and mutates the live in-memory ``self._path_indexes``
entry for orphan detection (STEP 1 dedup: a file's OLD point_ids not in
its NEW point_ids get evicted from ``chunks.db``), but -- unlike
``upsert_points()``'s SHARDED_JSON branch (Gap D) and both branches of
``delete_points()`` (Gap B) -- it has NO equivalent out-of-session persist
call at all. ``_upsert_points_chunks_db()`` returns directly (see
``upsert_points()``'s early dispatch), never reaching the SHARDED_JSON-only
Gap D code further down in the same method.

This test proves the on-disk bin goes stale across a process boundary:
build a CHUNKS_DB collection with one file via a normal session (bin
correctly reflects it), then have a genuinely FRESH store instance (no
``begin_indexing()`` call -- mirrors watch mode) replace that file's only
point with a NEW point_id via ``upsert_points()`` directly. Pre-fix, the
on-disk ``path_index.bin`` still shows the OLD point_id afterwards (or is
simply never updated) -- a THIRD process trusting that stale bin for
future orphan detection would fail to evict the truly-superseded point
and/or wrongly target the wrong id.

Real ``FilesystemVectorStore`` + real SQLite ``chunks.db`` throughout --
no mocking of the code under test.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)

from _pathindex_gap_1575_helpers import load_measurement_module, make_vector

FILE_PATH = "src/module_0.py"
NEW_POINT_ID = "pt_0_replacement"


def _build_one_file_chunks_db_fixture(tmp_path: Path):
    mut = load_measurement_module()
    return mut.build_synthetic_fixture(
        tmp_path / "chunks_db_gap_d",
        num_points=1,
        chunks_per_file=1,
        use_chunks_db=True,
    )


def _replace_the_file_with_no_active_session(
    base_path: Path, collection_name: str
) -> None:
    # A genuinely fresh store instance ("process 2") -- no begin_indexing()
    # ever called for this collection in this instance's lifetime.
    fresh_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=True
    )
    assert collection_name not in fresh_store._indexing_session_changes
    upsert_result = fresh_store.upsert_points(
        collection_name,
        [
            {
                "id": NEW_POINT_ID,
                "vector": make_vector(555),
                "payload": {
                    "path": FILE_PATH,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    assert upsert_result["status"] == "ok"


def test_chunks_db_out_of_session_upsert_persists_path_index(tmp_path):
    fixture = _build_one_file_chunks_db_fixture(tmp_path)
    base_path = fixture.base_path
    collection_name = fixture.collection_name
    old_point_id = fixture.point_ids_by_file[FILE_PATH][0]

    _replace_the_file_with_no_active_session(base_path, collection_name)

    # Read path_index.bin directly from disk (bypassing any in-memory
    # state) -- this is what the NEXT process would lazy-load and trust
    # for future orphan/dedup detection.
    collection_path = base_path / collection_name
    on_disk = PathIndex.load(collection_path / "path_index.bin")
    persisted_ids = on_disk.get_point_ids(FILE_PATH)

    assert persisted_ids == {NEW_POINT_ID}, (
        f"expected path_index.bin to reflect the out-of-session CHUNKS_DB "
        f"upsert's replacement point_id {{{NEW_POINT_ID!r}}} for "
        f"{FILE_PATH!r}, got {persisted_ids!r} (old point_id was "
        f"{old_point_id!r}) -- _upsert_points_chunks_db() must persist its "
        f"in-memory PathIndex update out-of-session, exactly like Gap D's "
        f"SHARDED_JSON branch and both branches of delete_points()'s Gap B, "
        f"or the on-disk bin goes stale across a process boundary and a "
        f"later process's orphan/dedup detection operates on wrong data."
    )
