"""Bug #1575 PathIndex-shortcut mechanism -- THIRD dual-review round, Gap A
(Codex, high confidence, reproduced): "a partial index gets trusted the NEXT
session."

Sequence: session 1 starts with NO ``path_index.bin`` on disk (e.g. a lost
file, or an index built before Story #540). ``_get_live_session_path_index``
correctly returns ``None`` (the ``_path_index_loaded_from_file`` flag is
False), so session 1's OWN ``unique_file_count`` answer correctly falls back
to a full authoritative scan. But nothing repaired the LIVE in-memory
``self._path_indexes`` entry that session 1 itself built (empty, then only
ever taught about the ONE file this session happened to touch) -- and
``end_indexing()`` unconditionally saves THAT partial live entry to
``path_index.bin``.

Session 2 then sees the bin now exists, trusts it via the fast path, and
durably persists the WRONG (catastrophically undercounted) unique_file_count.

Fix A closes this structurally: whenever the authoritative FULL scan is
computed as a fallback, it must REPAIR the live ``self._path_indexes`` entry
(and mark it ``_path_index_loaded_from_file = True``) so any SUBSEQUENT save
in the SAME or a later session persists the complete picture, not the
partial one.

Real ``FilesystemVectorStore`` + real filesystem/SQLite throughout -- no
mocking of the code under test. Reuses ``build_synthetic_fixture`` (via
``_pathindex_gap_1575_helpers``), the same synthetic-fixture builder the
permanent Finding-1 scaling test uses.
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
SESSION_1_VECTOR_SEED = 111
SESSION_2_VECTOR_SEED = 222


def _build_fixture_with_missing_bin(
    tmp_path: Path, *, use_chunks_db: bool, suffix: str
):
    mut = load_measurement_module()
    fixture = mut.build_synthetic_fixture(
        tmp_path / f"gap_a_{suffix}",
        num_points=TOTAL_FILE_COUNT,
        chunks_per_file=1,
        use_chunks_db=use_chunks_db,
    )
    collection_path = fixture.base_path / fixture.collection_name
    # Simulate a missing/lost path_index.bin for an otherwise fully
    # populated, real TOTAL_FILE_COUNT-file collection -- session 1 starts
    # blind.
    (collection_path / "path_index.bin").unlink()
    return fixture


def _run_session_touching_one_file(
    store: FilesystemVectorStore,
    *,
    collection_name: str,
    file_path: str,
    point_id: str,
    seed: int,
) -> None:
    store.begin_indexing(collection_name)
    upsert_result = store.upsert_points(
        collection_name,
        [
            {
                "id": point_id,
                "vector": make_vector(seed),
                "payload": {
                    "path": file_path,
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    assert upsert_result["status"] == "ok"
    store.end_indexing(collection_name)


def _run_two_session_scenario(
    tmp_path: Path, *, use_chunks_db: bool, suffix: str
) -> int:
    fixture = _build_fixture_with_missing_bin(
        tmp_path, use_chunks_db=use_chunks_db, suffix=suffix
    )
    collection_name = fixture.collection_name
    base_path = fixture.base_path
    collection_path = base_path / collection_name

    # Session 1: touches only ONE of the TOTAL_FILE_COUNT files. Its OWN
    # answer is already correctly computed via the authoritative fallback
    # (Round 2's fix) -- this test does not re-assert that; it continues on
    # to session 2, which is what Gap A is actually about.
    session1_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=use_chunks_db
    )
    _run_session_touching_one_file(
        session1_store,
        collection_name=collection_name,
        file_path=fixture.file_paths[0],
        point_id="pt_new_touch_s1",
        seed=SESSION_1_VECTOR_SEED,
    )

    # path_index.bin must now exist again (session 1's end_indexing() saved
    # SOME PathIndex to it) -- this is the precondition for session 2 to
    # even take the fast path.
    assert (collection_path / "path_index.bin").exists()

    # DISCRIMINATING ASSERTION (retrofit, SHARDED_JSON only): read
    # path_index.bin's actual persisted content right after session 1
    # completes, BEFORE session 2 runs its own end_indexing() -- for
    # SHARDED_JSON, session 2's end_indexing() ALSO always forces a fresh
    # authoritative rescan (_calculate_and_save_unique_file_count()'s
    # project-owner FINAL decision), which would self-heal path_index.bin
    # to the true count regardless of whether session 1's Gap A repair
    # itself worked. This proves session 1 persisted the COMPLETE picture
    # (all TOTAL_FILE_COUNT files), not just the 1 file it touched.
    #
    # CHUNKS_DB is deliberately NOT checked here:
    # _calculate_and_save_unique_file_count()'s CHUNKS_DB branch never
    # calls _rebuild_and_repair_path_index() at all -- it always answers
    # from chunk_store.distinct_paths() directly, so Gap A's live-PathIndex
    # -repair defect cannot manifest in CHUNKS_DB's unique_file_count
    # regardless of what path_index.bin (which CHUNKS_DB still writes, for
    # the separate Story #540 duplicate-prevention purpose) holds. The
    # final unique_file_count check below remains a plain sanity check for
    # that parametrization, not a Gap-A discriminator.
    if not use_chunks_db:
        session1_bin = PathIndex.load(collection_path / "path_index.bin")
        session1_paths = session1_bin.all_paths()
        assert len(session1_paths) == TOTAL_FILE_COUNT, (
            f"[SHARDED_JSON] expected path_index.bin to hold all "
            f"{TOTAL_FILE_COUNT} files immediately after session 1's "
            f"end_indexing(), got {len(session1_paths)}: "
            f"{sorted(session1_paths)} -- Gap A: session 1's live PathIndex "
            f"was never repaired with the authoritative fallback scan it "
            f"was forced to compute, so only the 1 file this session "
            f"touched got persisted."
        )

    # Session 2: on a genuinely fresh store instance (simulating a new
    # process), touching a DIFFERENT file than session 1.
    session2_store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=use_chunks_db
    )
    _run_session_touching_one_file(
        session2_store,
        collection_name=collection_name,
        file_path=fixture.file_paths[1],
        point_id="pt_new_touch_s2",
        seed=SESSION_2_VECTOR_SEED,
    )

    return int(read_unique_file_count(base_path, collection_name))


def _assert_reports_true_full_count(count: int, *, layout_label: str) -> None:
    assert count == TOTAL_FILE_COUNT, (
        f"expected unique_file_count=={TOTAL_FILE_COUNT} in session 2 ("
        f"{layout_label}, the true, complete collection picture) -- got "
        f"{count}. A session that starts with no path_index.bin on disk "
        f"must REPAIR the live in-memory PathIndex with the authoritative "
        f"full-scan result it was already forced to compute, so "
        f"end_indexing() never persists a partial picture that a LATER "
        f"session then wrongly trusts via the fast path."
    )


def test_gap_a_missing_bin_then_repaired_bin_sharded_json(tmp_path):
    """SHARDED_JSON: session 2 must report the TRUE full file count, never
    session 1's stale partial picture (which would surface as 1 or 2, not
    TOTAL_FILE_COUNT)."""
    count = _run_two_session_scenario(tmp_path, use_chunks_db=False, suffix="sharded")
    _assert_reports_true_full_count(count, layout_label="SHARDED_JSON")


def test_gap_a_missing_bin_then_repaired_bin_chunks_db(tmp_path):
    """CHUNKS_DB: identical scenario, chunk-store-backed layout."""
    count = _run_two_session_scenario(tmp_path, use_chunks_db=True, suffix="chunks_db")
    _assert_reports_true_full_count(count, layout_label="CHUNKS_DB")
