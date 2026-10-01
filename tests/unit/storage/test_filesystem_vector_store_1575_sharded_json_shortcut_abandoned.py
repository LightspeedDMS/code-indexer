"""Bug #1575 -- project-owner final architectural decision (after 6
consecutive dual-review rounds, each finding a NEW distinct correctness
bug in the SHARDED_JSON PathIndex fast-path mechanism: catastrophic
undercount, session leaks, non-atomic writes, TOCTOU races, corrupt-file
trust, write-ordering races, logical lost-updates, an object-swap silently
discarding concurrent mutations, and a stale-multi-writer gap with no
self-healing): the live-session PathIndex fast-path shortcut in
``FilesystemVectorStore._calculate_and_save_unique_file_count`` is ABANDONED
for SHARDED_JSON too, matching the treatment already given to CHUNKS_DB in
round 5's Fix 1 (see ``test_filesystem_vector_store_1575_chunks_db_revert.py``,
whose exact methodology this file mirrors for the SHARDED_JSON layout).

``_calculate_and_save_unique_file_count``'s SHARDED_JSON branch now ALWAYS
computes the count via an authoritative, from-disk PathIndex rebuild
(``_rebuild_and_repair_path_index``) -- it never consults
``_get_live_session_path_index``/``_resolve_authoritative_path_index``'s
cache-trusting fast path first. The rebuild-and-repair call is retained
(not replaced by a bare, side-effect-free disk scan) because it is ALSO
the mechanism that repairs ``self._path_indexes`` in place before
``end_indexing()``'s own subsequent ``path_index.bin`` save -- removing
that repair side effect would reintroduce the separate, already-fixed
"Gap A" catastrophic-undercount regression for Part B's (Story #540)
duplicate-prevention persistence, which is explicitly NOT being touched by
this change.

``test_sharded_json_ignores_stale_live_path_index_cache`` reproduces the
CHUNKS_DB revert test's "killed session leaves a stale-but-present cache"
scenario against a REAL ``FilesystemVectorStore`` + real filesystem (no
mocking of the code under test): it corrupts the live in-memory PathIndex
entry with a bogus phantom file that does NOT exist on disk, marks it
"proven complete" (``_path_index_loaded_from_file = True`` -- the exact
signal ``_get_live_session_path_index`` used to gate its now-removed
fast-path trust decision on), and confirms the count is STILL the TRUE,
disk-backed answer -- never the corrupted cache's inflated count.

``test_sharded_json_correct_regardless_of_path_index_bin_disk_state``
further proves the on-disk ``path_index.bin`` file's state (missing or
corrupted) has zero bearing on the correctness of the computed count for
SHARDED_JSON either.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 8
REAL_FILE_COUNT = 10
PHANTOM_FILE_PATH = "src/phantom_does_not_exist.py"
PHANTOM_POINT_ID = "pt_phantom_stale_cache_entry"


def _make_vector(seed: int):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()


def _build_sharded_json_collection(tmp_path: Path) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    collection_name = "coll"
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    store.begin_indexing(collection_name)
    try:
        points = [
            {
                "id": f"pt_{i}",
                "vector": _make_vector(i),
                "payload": {
                    "path": f"src/module_{i}.py",
                    "type": "content",
                    "hidden_branches": [],
                },
            }
            for i in range(REAL_FILE_COUNT)
        ]
        store.upsert_points(collection_name, points)
    finally:
        store.end_indexing(collection_name)
    return store


def _inject_stale_phantom_cache_entry_and_verify_precondition(
    store: FilesystemVectorStore, collection_name: str
) -> None:
    """Simulate a killed/crashed session that left a present-but-stale,
    "proven complete" in-memory PathIndex: inject a phantom file/point_id
    pair that does NOT exist on disk, mark the cache entry as fully
    loaded/trustworthy (the exact state ``_get_live_session_path_index``
    used to require before handing back the cached answer), and verify the
    corruption actually took hold -- proving this scenario is a genuine,
    discriminating reproduction, not a vacuous check.
    """
    cache_key = store._id_cache_key(collection_name, None)
    with store._path_index_lock:
        path_index = store._path_indexes[cache_key]
        path_index.add_point(PHANTOM_FILE_PATH, PHANTOM_POINT_ID)
        store._path_index_loaded_from_file[cache_key] = True

    live_index = store._get_live_session_path_index(collection_name, None)
    assert live_index is not None, (
        "the live-session PathIndex must be considered 'proven complete' "
        "in this scenario for the phantom-entry corruption to be a "
        "meaningful, discriminating test of the SHARDED_JSON abandonment"
    )
    assert len(live_index.all_paths()) == REAL_FILE_COUNT + 1, (
        "the corrupted live cache must actually contain the phantom entry "
        "(this is the pre-condition the fix must be proven to ignore)"
    )


def test_sharded_json_ignores_stale_live_path_index_cache(tmp_path):
    store = _build_sharded_json_collection(tmp_path)
    collection_name = "coll"
    collection_path = tmp_path / collection_name

    store.begin_indexing(collection_name)
    try:
        _inject_stale_phantom_cache_entry_and_verify_precondition(
            store, collection_name
        )

        unique_file_count = store._calculate_and_save_unique_file_count(
            collection_name, collection_path
        )

        assert unique_file_count == REAL_FILE_COUNT, (
            f"expected the TRUE disk-backed count ({REAL_FILE_COUNT}), got "
            f"{unique_file_count}. For SHARDED_JSON collections, "
            f"_calculate_and_save_unique_file_count must ALWAYS compute the "
            f"authoritative, from-disk count and must NEVER trust the "
            f"live-session PathIndex cache -- a corrupted/stale cache (as a "
            f"killed/crashed session would leave behind) must have zero "
            f"effect on the computed count for this layout, exactly like "
            f"CHUNKS_DB's round-5 revert."
        )
    finally:
        store.end_indexing(collection_name)


@pytest.mark.parametrize(
    "disk_state",
    ["missing", "corrupted_bytes"],
    ids=["path_index_bin_missing", "path_index_bin_corrupted"],
)
def test_sharded_json_correct_regardless_of_path_index_bin_disk_state(
    tmp_path, disk_state
):
    """SHARDED_JSON must compute the correct unique_file_count regardless
    of path_index.bin's on-disk state -- missing entirely, or present but
    corrupted -- because the authoritative rebuild self-heals from the
    real vector_*.json files on disk regardless. Uses a FRESH store
    instance (no in-memory state at all) to simulate a brand-new process
    that never had a live PathIndex to begin with."""
    _build_sharded_json_collection(tmp_path)
    collection_name = "coll"
    collection_path = tmp_path / collection_name
    path_index_bin = collection_path / "path_index.bin"

    if disk_state == "missing":
        path_index_bin.unlink()
    else:
        path_index_bin.write_bytes(b"not a valid msgpack payload at all \x00\xff")

    fresh_store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    unique_file_count = fresh_store._calculate_and_save_unique_file_count(
        collection_name, collection_path
    )
    assert unique_file_count == REAL_FILE_COUNT, (
        f"expected {REAL_FILE_COUNT} (authoritative disk rebuild), got "
        f"{unique_file_count} -- SHARDED_JSON's unique_file_count "
        f"computation must self-heal regardless of path_index.bin's "
        f"on-disk state ({disk_state})"
    )
