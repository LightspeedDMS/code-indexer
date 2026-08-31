"""Regression test for a dual-review-confirmed (opus-only, independently
reproduced, MORE SEVERE variant) defect INTRODUCED by Bug #1575 Finding 1's
own fix (the live-session ``PathIndex`` shortcut in
``FilesystemVectorStore._calculate_and_save_unique_file_count``).

If ``path_index.bin`` is missing when ``begin_indexing()`` runs, an EMPTY
PathIndex is created. A session that then touches only a handful of files
(the incremental-refresh common case) builds a PathIndex containing ONLY
those files. Taking the fast path in that situation reports the session's
own touched-file count as though it were the WHOLE collection's
unique_file_count -- a severe undercount (``unique_file_count`` is
documented in this project's CLAUDE.md, Story #1459 AC3, as one of three
scalars every downstream reader trusts without re-deriving, and a
low/zero value is treated elsewhere as a broken-index signal).

Real ``FilesystemVectorStore`` + real filesystem/SQLite throughout -- no
mocking of the code under test. Reuses ``build_synthetic_fixture`` (via
``_pathindex_gap_1575_helpers``), the same synthetic-fixture builder the
permanent Finding-1 scaling test uses.
"""

from __future__ import annotations

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from _pathindex_gap_1575_helpers import (
    load_measurement_module,
    make_vector,
    read_unique_file_count,
)


def _run_missing_path_index_scenario(tmp_path, *, use_chunks_db: bool, suffix: str):
    mut = load_measurement_module()
    fixture = mut.build_synthetic_fixture(
        tmp_path / f"missing_path_index_{suffix}",
        num_points=10,
        chunks_per_file=1,
        use_chunks_db=use_chunks_db,
    )
    collection_name = fixture.collection_name
    base_path = fixture.base_path
    collection_path = base_path / collection_name
    touched_file = fixture.file_paths[0]

    # Simulate a missing/lost path_index.bin (e.g. pre-Story-#540 index, or
    # a lost/corrupted file) for an otherwise fully-populated, real
    # 10-file collection.
    (collection_path / "path_index.bin").unlink()

    # Fresh store instance: begin_indexing() will build an EMPTY PathIndex
    # (file missing) rather than reusing any in-memory state.
    fresh_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=use_chunks_db
    )
    fresh_store.begin_indexing(collection_name)
    upsert_result = fresh_store.upsert_points(
        collection_name,
        [
            {
                "id": "pt_new_touch",
                "vector": make_vector(12345),
                "payload": {
                    "path": touched_file,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    assert upsert_result["status"] == "ok"
    fresh_store.end_indexing(collection_name)

    return read_unique_file_count(base_path, collection_name)


def test_missing_path_index_bin_sharded_json_reports_true_full_count(tmp_path):
    count = _run_missing_path_index_scenario(
        tmp_path, use_chunks_db=False, suffix="sharded"
    )
    assert count == 10, (
        f"expected the TRUE full collection unique_file_count (10) when "
        f"path_index.bin was missing at begin_indexing() time and the "
        f"session only touched 1 of the 10 files -- got {count}. Trusting "
        f"a PathIndex that was built from scratch this session (never "
        f"loaded from an existing file) as though it were the whole "
        f"collection's picture is the root cause of this catastrophic "
        f"undercount."
    )


def test_missing_path_index_bin_chunks_db_reports_true_full_count(tmp_path):
    count = _run_missing_path_index_scenario(
        tmp_path, use_chunks_db=True, suffix="chunks_db"
    )
    assert count == 10, (
        f"expected the TRUE full collection unique_file_count (10) when "
        f"path_index.bin was missing at begin_indexing() time and the "
        f"session only touched 1 of the 10 files (CHUNKS_DB layout) -- got "
        f"{count}."
    )
