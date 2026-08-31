"""Regression tests for two dual-review-confirmed (Claude opus + Codex,
independently) delete-path defects INTRODUCED by Bug #1575 Finding 1's own
fix (the live-session ``PathIndex`` shortcut in
``FilesystemVectorStore._calculate_and_save_unique_file_count``):

1. CHUNKS_DB ``delete_points()`` never updates ``self._path_indexes`` at
   all -- deleting every chunk of a file leaves the live PathIndex still
   claiming that file exists, so the fast-path unique_file_count is
   STALE (too high) and gets durably persisted to ``collection_meta.json``.

2. SHARDED_JSON ``delete_points()`` DOES update the path index, but only
   ``if _delete_points_cache_key in self._path_indexes`` -- a silent
   no-op skip when that key was never lazily populated first (the normal
   state when ``delete_points()`` is called with no prior
   ``begin_indexing()``/upsert in this process -- exactly
   ``delete_by_filter()``'s real call pattern). The skip leaves the
   in-memory cache untouched (not even loaded), so a SUBSEQUENT
   ``begin_indexing()`` loads the STALE on-disk ``path_index.bin`` (never
   told about the deletion either) and the deleted file resurfaces.

Real ``FilesystemVectorStore`` + real filesystem/SQLite throughout -- no
mocking of the code under test. Reuses ``build_synthetic_fixture`` (via
``_pathindex_gap_1575_helpers``), the same synthetic-fixture builder the
permanent Finding-1 scaling test uses.
"""

from __future__ import annotations

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

from _pathindex_gap_1575_helpers import load_measurement_module, read_unique_file_count


def test_chunks_db_delete_all_chunks_of_a_file_updates_unique_file_count(tmp_path):
    """Delete ALL chunks of one file (of two) inside an active indexing
    session; the resulting unique_file_count must reflect the TRUE
    remaining distinct-path count (1), never the stale pre-delete count
    (2)."""
    mut = load_measurement_module()
    fixture = mut.build_synthetic_fixture(
        tmp_path / "chunks_db_delete",
        num_points=4,
        chunks_per_file=2,
        use_chunks_db=True,
    )
    store = fixture.store
    collection_name = fixture.collection_name
    file_to_delete = fixture.file_paths[0]
    ids_to_delete = fixture.point_ids_by_file[file_to_delete]
    assert len(ids_to_delete) == 2

    store.begin_indexing(collection_name)
    delete_result = store.delete_points(collection_name, ids_to_delete)
    assert delete_result["status"] == "ok"
    assert delete_result["deleted"] == 2
    store.end_indexing(collection_name)

    count = read_unique_file_count(fixture.base_path, collection_name)
    assert count == 1, (
        f"expected unique_file_count==1 after deleting the only remaining "
        f"file's worth of chunks for {file_to_delete!r}, got {count} -- the "
        f"CHUNKS_DB delete_points() branch must keep the live in-memory "
        f"PathIndex in sync (mirroring what the SHARDED_JSON branch already "
        f"does), or the fast-path unique_file_count shortcut reports the "
        f"stale pre-delete picture"
    )


def test_sharded_json_delete_during_active_session_stays_correct(tmp_path):
    """Guard test: the pre-existing 'OK' in-session case (delete_points()
    called AFTER begin_indexing() already lazily populated the path index)
    must stay correct -- this is NOT expected to regress."""
    mut = load_measurement_module()
    fixture = mut.build_synthetic_fixture(
        tmp_path / "sharded_delete_ok",
        num_points=4,
        chunks_per_file=2,
        use_chunks_db=False,
    )
    store = fixture.store
    collection_name = fixture.collection_name
    file_to_delete = fixture.file_paths[0]
    ids_to_delete = fixture.point_ids_by_file[file_to_delete]

    store.begin_indexing(collection_name)
    delete_result = store.delete_points(collection_name, ids_to_delete)
    assert delete_result["status"] == "ok"
    assert delete_result["deleted"] == 2
    store.end_indexing(collection_name)

    count = read_unique_file_count(fixture.base_path, collection_name)
    assert count == 1


def test_sharded_json_delete_without_prior_lazy_population_closes_silent_skip_gap(
    tmp_path,
):
    """delete_points() called with NO begin_indexing()/upsert_points() ever
    having lazily populated self._path_indexes for this collection in this
    process -- mirroring delete_by_filter()'s real call pattern (it calls
    delete_points() directly with no surrounding indexing session).

    The pre-fix guard (``if cache_key in self._path_indexes``) silently
    skips the update in this exact situation (the key was never
    populated), so the stale on-disk path_index.bin resurfaces the
    deleted file on the very next session that touches this collection.
    """
    mut = load_measurement_module()
    fixture = mut.build_synthetic_fixture(
        tmp_path / "sharded_delete_silent_skip",
        num_points=4,
        chunks_per_file=2,
        use_chunks_db=False,
    )
    collection_name = fixture.collection_name
    base_path = fixture.base_path
    file_to_delete = fixture.file_paths[0]
    ids_to_delete = fixture.point_ids_by_file[file_to_delete]

    # Fresh store instance: its self._path_indexes is empty for this
    # collection -- nothing has lazily populated it yet in this process.
    fresh_store = FilesystemVectorStore(base_path=base_path)
    delete_result = fresh_store.delete_points(collection_name, ids_to_delete)
    assert delete_result["status"] == "ok"
    assert delete_result["deleted"] == 2

    # Open a session on the SAME fresh_store instance with no further
    # mutations, to surface whichever picture begin_indexing() ends up
    # trusting (in-memory carry-over from delete_points() if the gap is
    # closed, or a stale disk reload if it is not).
    fresh_store.begin_indexing(collection_name)
    fresh_store.end_indexing(collection_name)

    count = read_unique_file_count(base_path, collection_name)
    assert count == 1, (
        f"expected unique_file_count==1 after delete_points() removed the "
        f"only chunks of {file_to_delete!r} -- got {count}. A delete_points() "
        f"call with no prior lazy population of self._path_indexes must "
        f"still keep the path index correct (by loading it first), never "
        f"silently skip the update"
    )
