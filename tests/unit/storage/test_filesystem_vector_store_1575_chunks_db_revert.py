"""Bug #1575 -- project-owner scoping decision (4th dual-review round):
for CHUNKS_DB collections, ``_calculate_and_save_unique_file_count`` no
longer trusts the live-session PathIndex fast-path shortcut AT ALL. It
always queries ``ChunkStore.distinct_paths()`` directly -- the ORIGINAL,
pre-#1575 behavior for this layout -- because that direct query was
measured at ~4.5ms even on a 24,000-row collection: the shortcut never
bought anything meaningful for CHUNKS_DB, while introducing a real
regression the dual review confirmed: a killed/crashed indexing session
leaves ``self._path_indexes``'s in-memory entry (and/or an on-disk
``path_index.bin`` written by an unrelated earlier call) present-but-stale,
and the fast path would trust it FOREVER for this layout (unlike
SHARDED_JSON, which self-heals via ``_resolve_authoritative_path_index``'s
authoritative-rebuild-and-repair fallback).

``test_chunks_db_ignores_stale_live_path_index_cache`` reproduces exactly
that "killed session leaves a stale-but-present cache" scenario against a
REAL ``FilesystemVectorStore`` + real SQLite ``chunks.db`` (no mocking of
the code under test): it corrupts the live in-memory PathIndex entry with a
bogus phantom file that does NOT exist in chunks.db, marks it "proven
complete" (``_path_index_loaded_from_file = True``, the exact signal
``_get_live_session_path_index`` gates its fast-path trust decision on),
and confirms the count is STILL the TRUE ``chunks.db``-backed answer.

``test_chunks_db_correct_regardless_of_path_index_bin_disk_state`` further
proves the on-disk ``path_index.bin`` file is neither required to exist
nor even readable for CHUNKS_DB to compute the correct answer (evidence
the mechanism is fully bypassed for this layout, not merely made more
cautious).
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


def _build_chunks_db_collection(tmp_path: Path) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
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
    pair that does NOT exist in chunks.db, mark the cache entry as fully
    loaded/trustworthy (the exact state _get_live_session_path_index
    requires before handing back the cached answer), and verify the
    corruption actually took hold -- proving this scenario is a genuine,
    discriminating reproduction of the regression, not a vacuous check.
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
        "meaningful, discriminating test of the CHUNKS_DB revert"
    )
    assert len(live_index.all_paths()) == REAL_FILE_COUNT + 1, (
        "the corrupted live cache must actually contain the phantom entry "
        "(this is the pre-condition the fix must be proven to ignore)"
    )


def test_chunks_db_ignores_stale_live_path_index_cache(tmp_path):
    store = _build_chunks_db_collection(tmp_path)
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
            f"expected the TRUE chunks.db-backed count "
            f"({REAL_FILE_COUNT}), got {unique_file_count}. For CHUNKS_DB "
            f"collections, _calculate_and_save_unique_file_count must "
            f"ALWAYS query ChunkStore.distinct_paths() directly and must "
            f"NEVER trust the live-session PathIndex cache -- a "
            f"corrupted/stale cache (as a killed/crashed session would "
            f"leave behind) must have zero effect on the computed count "
            f"for this layout."
        )
    finally:
        store.end_indexing(collection_name)


@pytest.mark.parametrize(
    "disk_state",
    ["missing", "corrupted_bytes"],
    ids=["path_index_bin_missing", "path_index_bin_corrupted"],
)
def test_chunks_db_correct_regardless_of_path_index_bin_disk_state(
    tmp_path, disk_state
):
    """CHUNKS_DB must compute the correct unique_file_count regardless of
    path_index.bin's on-disk state -- missing entirely, or present but
    corrupted -- because that branch never reads the file at all. Uses a
    FRESH store instance (no in-memory state at all) to simulate a
    brand-new process that never had a live PathIndex to begin with."""
    _build_chunks_db_collection(tmp_path)
    collection_name = "coll"
    collection_path = tmp_path / collection_name
    path_index_bin = collection_path / "path_index.bin"

    if disk_state == "missing":
        path_index_bin.unlink()
    else:
        path_index_bin.write_bytes(b"not a valid msgpack payload at all \x00\xff")

    fresh_store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=True
    )
    unique_file_count = fresh_store._calculate_and_save_unique_file_count(
        collection_name, collection_path
    )
    assert unique_file_count == REAL_FILE_COUNT, (
        f"expected {REAL_FILE_COUNT} (direct chunks.db query), got "
        f"{unique_file_count} -- CHUNKS_DB's unique_file_count computation "
        f"must be entirely independent of path_index.bin's on-disk state "
        f"({disk_state})"
    )
