"""Bug #1575 PathIndex-shortcut mechanism -- Part 2 of the round-7
follow-up: ``scroll_points()``'s lazy-rebuild fast path gained a
STRUCTURAL mirror of ``_rebuild_and_repair_path_index()``'s before/after-
snapshot prune fix (both now call the shared
``_merge_rebuilt_path_index_with_prune()``), closing the docstring's
pre-existing (and, until this change, INACCURATE) parity claim.

This module proves TWO separate, honestly-distinguished things:

1. The underlying merge+prune mechanism (``_merge_rebuilt_path_index_
   with_prune``) is CORRECT IN GENERAL: given a non-empty ``before_
   snapshot``, it prunes a point that was concurrently removed from the
   live object during a (simulated) scan window, exactly like
   ``_rebuild_and_repair_path_index()``'s own round-7 test proves for its
   call site.

2. For ``scroll_points()``'s SPECIFIC ``needs_rebuild`` gate
   (``needs_rebuild = not path_index._path_index``), the mirror is a
   PROVABLE STRUCTURAL NO-OP in practice: ``needs_rebuild`` can only be
   True when the live PathIndex dict is ALREADY EMPTY at the moment it is
   checked (same lock acquisition as the ``before_snapshot`` capture), so
   ``before_snapshot`` is ALWAYS ``{}`` for every real firing of this
   branch. A point that is added to the live object and removed again
   ENTIRELY within the (unlocked) disk-scan window -- starting from this
   genuinely-empty cache -- is therefore NOT protected: the prune step has
   nothing in ``before_snapshot`` to compare against, so a stale disk scan
   that still has the point resurrects it via the plain merge.

Neither claim is invented: both are reproduced deterministically via
direct object construction (this codebase's own established methodology,
see the round-7 test module docstrings), never mocking the code under
test, and never relying on real thread-scheduling timing.

A third test proves the WIRING itself: a real, end-to-end
``scroll_points()`` call against a real legacy (missing ``path_index.bin``)
collection still returns the correct result after the refactor -- i.e. the
mirror did not regress the existing, byte-identical-for-every-real-caller
behavior.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)

from _pathindex_gap_1575_helpers import load_measurement_module

TOTAL_FILE_COUNT = 5
VECTOR_SIZE = 8
SCROLL_LIMIT = 100
COLLECTION_NAME = "coll"


def _build_fixture(tmp_path: Path, *, suffix: str):
    mut = load_measurement_module()
    return mut.build_synthetic_fixture(
        tmp_path / f"scroll_merge_prune_{suffix}",
        num_points=TOTAL_FILE_COUNT,
        chunks_per_file=1,
        use_chunks_db=False,
    )


def _new_store_with_cache_key(tmp_path: Path):
    """Real store + real collection, returning (store, cache_key) for the
    two direct-merge-helper tests below."""
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(COLLECTION_NAME, vector_size=VECTOR_SIZE)
    cache_key = store._id_cache_key(COLLECTION_NAME, None)
    return store, cache_key


def test_merge_rebuilt_path_index_with_prune_general_mechanism_prunes_concurrent_deletion(
    tmp_path,
):
    """Claim 1 (general correctness): a non-empty before_snapshot lets the
    shared helper prune a point concurrently removed from the live object
    during the scan window, even though the (simulated stale) rebuilt scan
    still has it -- see module docstring for full rationale."""
    store, cache_key = _new_store_with_cache_key(tmp_path)

    live = PathIndex()
    live.add_point("src/kept.py", "pt_kept")
    live.add_point("src/deleted.py", "pt_deleted")
    with store._path_index_lock:
        store._path_indexes[cache_key] = live
        before_snapshot = store._path_indexes[cache_key].snapshot()
    assert before_snapshot == {
        "src/kept.py": {"pt_kept"},
        "src/deleted.py": {"pt_deleted"},
    }

    live.remove_point("src/deleted.py", "pt_deleted")  # concurrent delete

    rebuilt = PathIndex()  # stale scan: still has the deleted point
    rebuilt.add_point("src/kept.py", "pt_kept")
    rebuilt.add_point("src/deleted.py", "pt_deleted")

    with store._path_index_lock:
        result = store._merge_rebuilt_path_index_with_prune(
            cache_key, before_snapshot, rebuilt
        )

    assert "pt_deleted" not in result.get_point_ids("src/deleted.py"), (
        "expected the concurrently-deleted point to be PRUNED despite the "
        "stale disk scan still having it."
    )
    assert "pt_kept" in result.get_point_ids("src/kept.py")


def test_scroll_points_needs_rebuild_residual_gap_transient_point_not_pruned(tmp_path):
    """Claim 2 (residual gap): reproduces scroll_points()'s real
    needs_rebuild==True precondition (before_snapshot == {}), then shows a
    point added-then-removed entirely within the scan window is NOT
    pruned -- see module docstring for full rationale."""
    store, cache_key = _new_store_with_cache_key(tmp_path)

    live = PathIndex()  # needs_rebuild's own precondition: EMPTY dict
    with store._path_index_lock:
        store._path_indexes[cache_key] = live
        path_index = store._path_indexes[cache_key]
        needs_rebuild = not path_index._path_index
        assert needs_rebuild is True
        before_snapshot = path_index.snapshot() if needs_rebuild else None
    assert before_snapshot == {}

    live.add_point("src/transient.py", "pt_transient")  # born ...
    live.remove_point("src/transient.py", "pt_transient")  # ... and died

    rebuilt = PathIndex()  # stale mid-window scan still has it
    rebuilt.add_point("src/transient.py", "pt_transient")

    with store._path_index_lock:
        result = store._merge_rebuilt_path_index_with_prune(
            cache_key, before_snapshot, rebuilt
        )

    assert "pt_transient" in result.get_point_ids("src/transient.py"), (
        "expected the transient point to be RESURRECTED by the stale scan "
        "-- an empty before_snapshot has nothing to prune against. If "
        "this ever fails instead, the residual gap has closed elsewhere "
        "and this test's documentation must be updated."
    )


def test_scroll_points_integration_legacy_collection_lazy_rebuild_still_correct(
    tmp_path,
):
    """Wiring/regression proof: a real, end-to-end scroll_points() call
    against a real legacy (missing path_index.bin) collection still
    returns the correct result after the refactor."""
    fixture = _build_fixture(tmp_path, suffix="integration")
    collection_name = fixture.collection_name
    base_path = fixture.base_path
    target_file = fixture.file_paths[0]
    expected_point_ids = fixture.point_ids_by_file[target_file]

    (base_path / collection_name / "path_index.bin").unlink()

    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    points, _next_offset = store.scroll_points(
        collection_name,
        limit=SCROLL_LIMIT,
        with_payload=True,
        filter_conditions={"must": [{"key": "path", "match": {"value": target_file}}]},
    )

    returned_ids = {p["id"] for p in points}
    assert returned_ids == set(expected_point_ids), (
        f"expected scroll_points() to return exactly {expected_point_ids} "
        f"for {target_file!r}, got {returned_ids}"
    )
