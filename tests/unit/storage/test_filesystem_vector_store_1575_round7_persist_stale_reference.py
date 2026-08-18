"""Bug #1575 PathIndex-shortcut mechanism -- round 7 (opus review),
distinct gap from the swap-vs-merge defect fixed in
``test_filesystem_vector_store_1575_round7_repair_swap_discards_concurrent_add.py``:
``_persist_out_of_session_path_index()`` persists the ``PathIndex``
reference its CALLER captured under a separate, already-released
``_path_index_lock`` acquisition, instead of re-reading
``self._path_indexes[cache_key]`` under the SAME lock acquisition it uses
to read ``self._path_index_loaded_from_file[cache_key]``.

All four production call sites (``upsert_points()``'s CHUNKS_DB and
SHARDED_JSON branches, ``delete_points()``'s CHUNKS_DB and SHARDED_JSON
branches) follow the identical pattern:

    with self._path_index_lock:
        captured = self._path_indexes.get(cache_key)
    self._persist_out_of_session_path_index(
        collection_name, cache_key, captured, subdirectory
    )

Between the lock being released after the capture and
``_persist_out_of_session_path_index`` actually calling
``_save_path_index()``, ANOTHER thread/call could have moved
``self._path_indexes[cache_key]`` forward (e.g. a concurrent
out-of-session mutation for the SAME collection, or a repair triggered by
another caller). Persisting the caller's stale, pre-advancement reference
instead of the current authoritative one writes a regression to
``path_index.bin`` -- silently overwriting whatever more-complete picture
was already live.

This test reproduces the END STATE of that race deterministically (same
established methodology as every other round-N test in this family):
capture a reference exactly like a real call site does, then simulate a
concurrent replacement of the live dict entry BEFORE calling the persist
method with the now-stale captured reference.

Real ``FilesystemVectorStore`` + real filesystem throughout -- no mocking
of the code under test.
"""

from __future__ import annotations

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)

COLLECTION_NAME = "coll"
STALE_FILE_PATH = "src/stale_only.py"
STALE_POINT_ID = "pt_stale"
CURRENT_FILE_PATH = "src/current_only.py"
CURRENT_POINT_ID = "pt_current"


def _build_store(tmp_path):
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(COLLECTION_NAME, vector_size=8)
    return store


def test_persist_out_of_session_uses_current_live_reference_not_stale_capture(
    tmp_path,
):
    store = _build_store(tmp_path)
    cache_key = store._id_cache_key(COLLECTION_NAME, None)

    stale = PathIndex()
    stale.add_point(STALE_FILE_PATH, STALE_POINT_ID)

    current = PathIndex()
    current.add_point(CURRENT_FILE_PATH, CURRENT_POINT_ID)

    with store._path_index_lock:
        store._path_indexes[cache_key] = stale
        # Proven-complete so _persist_out_of_session_path_index takes the
        # direct-save branch rather than forcing a rebuild-and-repair.
        store._path_index_loaded_from_file[cache_key] = True

    # Mirrors a real call site's own capture-under-lock.
    with store._path_index_lock:
        captured = store._path_indexes.get(cache_key)
    assert captured is stale

    # Simulate a concurrent replacement landing in the window between the
    # caller's capture and the persist call actually running -- e.g.
    # another thread's own out-of-session mutation for the SAME cache_key.
    with store._path_index_lock:
        store._path_indexes[cache_key] = current

    # NOTE: the production fix for this exact gap removed the caller-
    # supplied ``path_index`` parameter entirely (it re-reads
    # self._path_indexes[cache_key] internally instead) -- ``captured`` is
    # kept above only to prove/document what a real call site captures
    # under its own lock, mirroring the pattern this test targets.
    del captured
    store._persist_out_of_session_path_index(
        COLLECTION_NAME, cache_key, subdirectory=None
    )

    persisted = PathIndex.load(
        store._get_collection_path(COLLECTION_NAME, None) / "path_index.bin"
    )
    assert CURRENT_POINT_ID in persisted.get_point_ids(CURRENT_FILE_PATH), (
        f"expected the CURRENT live picture ({CURRENT_POINT_ID!r} for "
        f"{CURRENT_FILE_PATH!r}) to be what gets persisted -- "
        f"_persist_out_of_session_path_index() must re-read "
        f"self._path_indexes[cache_key] under its own lock acquisition at "
        f"persist time, not trust a reference the caller captured earlier "
        f"under a separate, already-released lock acquisition."
    )
    assert STALE_POINT_ID not in persisted.get_point_ids(STALE_FILE_PATH), (
        f"persisted path_index.bin still reflects the STALE captured "
        f"reference ({STALE_POINT_ID!r} for {STALE_FILE_PATH!r}) instead of "
        f"the current live picture -- a stale-reference regression."
    )
