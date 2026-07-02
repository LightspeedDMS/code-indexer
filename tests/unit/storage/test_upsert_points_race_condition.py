"""Tests for Bug #663: Race condition in upsert_points orphan deletion.

STEP 3 of orphan deletion previously performed an unconditional _id_index
deletion when a concurrent thread could have already re-populated the same
point_id with a new path. This caused shared chunks to be silently dropped
from the HNSW index.

The fix: guard the _id_index deletion in STEP 3 by checking path equality.
Only evict if the stored path still matches the orphan's vector_file gathered
in STEP 1. If the path has changed, a concurrent thread re-populated the
entry and we must NOT delete it.

Test strategy:
- All index state observations use the public load_id_index() API after
  end_indexing() persists the in-memory _id_index to disk.
- Internal _id_index access is limited to the injection helper (Thread B
  simulation), which requires a write that has no public API.
- Observable correctness is checked via: index presence/absence (load_id_index)
  and absence of HNSW warning logs (caplog).
"""

import logging
from pathlib import Path

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


COLLECTION = "test_collection"
VECTOR_SIZE = 64  # Small vectors for speed

# Deterministic vectors — same content every run
VECTOR_A = [0.1] * VECTOR_SIZE
VECTOR_B = [0.2] * VECTOR_SIZE
VECTOR_C = [0.3] * VECTOR_SIZE


def make_store(tmp_path: Path) -> FilesystemVectorStore:
    """Create a fresh FilesystemVectorStore in tmp_path."""
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection(COLLECTION, vector_size=VECTOR_SIZE)
    store.begin_indexing(COLLECTION)
    return store


def make_point(point_id: str, file_path: str, vector: list) -> dict:
    """Create a point dict with deterministic vector content."""
    return {
        "id": point_id,
        "vector": vector,
        "payload": {"path": file_path, "chunk_index": 0},
    }


def _install_unlink_injector(
    monkeypatch,
    store: FilesystemVectorStore,
    injected_path: Path,
    target_point_id: str,
) -> None:
    """Patch Path.unlink to simulate Thread B re-writing a point_id to _id_index.

    When STEP 2 deletes a vector file, this hook fires immediately after the
    deletion, injecting a new path into _id_index[COLLECTION][target_point_id].
    This simulates a concurrent thread having re-written the same point_id
    between STEP 2 (file delete) and STEP 3 (index update).

    Scoped via monkeypatch — auto-restored after the test.
    """
    original_unlink = Path.unlink

    def injecting_unlink(self_path: Path, missing_ok: bool = False) -> None:
        original_unlink(self_path, missing_ok=missing_ok)
        # Simulate Thread B: create a new file and register it in _id_index
        injected_path.parent.mkdir(parents=True, exist_ok=True)
        injected_path.touch()
        with store._id_index_lock:
            if COLLECTION not in store._id_index:
                store._id_index[COLLECTION] = {}
            store._id_index[COLLECTION][target_point_id] = injected_path

    monkeypatch.setattr(Path, "unlink", injecting_unlink)


class TestStep3GuardSkipsDeletionWhenPathSuperseded:
    """Test 1: When _id_index[collection][orphan_id] was re-written by a concurrent
    thread BETWEEN STEP 1 and STEP 3, STEP 3 must NOT delete the entry.

    Observable: after end_indexing, the point is present in the persisted index.
    """

    def test_step3_guard_skips_deletion_when_path_superseded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Simulate race: _id_index entry updated between STEP 1 and STEP 3.

        Setup:
          - File F_A has point P indexed.
          - upsert_points for F_A with a different point makes P an orphan.
          - Between STEP 2 (file delete) and STEP 3 (index update), Thread B
            re-writes _id_index[COLLECTION][P] to a new path (injector fires).
        Expect (after end_indexing):
          - load_id_index still contains P (not evicted by STEP 3).
        """
        store = make_store(tmp_path)

        # Index F_A with point P
        store.upsert_points(
            COLLECTION, [make_point("shared_chunk_P", "src/file_a.py", VECTOR_A)]
        )

        # Prepare the fake path Thread B will inject
        injected_path = tmp_path / COLLECTION / "fake_new_vector_shared_chunk_P.json"

        # Install unlink injector (scoped via monkeypatch)
        _install_unlink_injector(monkeypatch, store, injected_path, "shared_chunk_P")

        # Upsert F_A with a different point — P becomes orphan, injector fires in STEP 2
        store.upsert_points(
            COLLECTION, [make_point("different_point_Q", "src/file_a.py", VECTOR_B)]
        )

        # end_indexing persists _id_index to disk (required for load_id_index to reflect state)
        store.end_indexing(COLLECTION)

        # P must survive: Thread B re-populated it, STEP 3 must not have evicted it
        assert "shared_chunk_P" in store.load_id_index(COLLECTION), (
            "Bug #663: STEP 3 must NOT delete _id_index[P] when a concurrent thread "
            "re-populated it with a new path between STEP 1 and STEP 3"
        )


class TestStep3GuardDeletesWhenPathUnchanged:
    """Test 2 (regression): Normal orphan deletion must still work correctly.

    When _id_index path matches the orphan's vector_file from STEP 1,
    the entry MUST be deleted from the persisted index.
    """

    def test_step3_guard_deletes_when_path_unchanged(self, tmp_path: Path) -> None:
        """Normal orphan deletion: no concurrent interference.

        Setup:
          - File F_A has point P.
          - Upsert F_A with a different point (making P orphaned).
          - No concurrent thread modifies _id_index.
        Expect (after end_indexing):
          - load_id_index does NOT contain P.
          - load_id_index DOES contain Q.
        """
        store = make_store(tmp_path)

        # Index F_A with point P
        store.upsert_points(
            COLLECTION, [make_point("orphan_P", "src/file_a.py", VECTOR_A)]
        )

        # Upsert F_A with a completely different point — P becomes orphan
        store.upsert_points(
            COLLECTION, [make_point("new_Q", "src/file_a.py", VECTOR_B)]
        )

        # Persist state
        store.end_indexing(COLLECTION)

        # P must have been removed (normal orphan cleanup)
        assert "orphan_P" not in store.load_id_index(COLLECTION), (
            "P should be absent from the persisted index after normal orphan cleanup"
        )

        # Q must be present (new point was written)
        assert "new_Q" in store.load_id_index(COLLECTION), (
            "Q must be present in the persisted index after upsert"
        )


class TestNoHnswWarningWhenSharedPointId:
    """Test 3: End-to-end — no HNSW warning emitted during end_indexing when a
    shared point_id is orphaned in one upsert but still owned by another file.

    The warning that must NOT appear:
        'Vector file not found for point <id>, skipping'
    emitted by _apply_incremental_hnsw_batch_update when a point_id appears
    in changes["added"] but its vector file is absent from _id_index.
    """

    def test_no_hnsw_warning_when_shared_point_id(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Sequential upserts simulate the shared-point-id race:

        1. Upsert F_A with [P] — P indexed for F_A.
        2. Upsert F_B with [P] — P also indexed for F_B (same point_id, different file).
           _id_index[P] is overwritten by F_B's path.
        3. Upsert F_A with [Q] — P is orphaned for F_A.
           STEP 3 path-equality guard: F_B's path differs from F_A's recorded path
           → P must NOT be deleted from _id_index.

        After end_indexing: P still in persisted index, no HNSW warning for P.
        """
        store = make_store(tmp_path)

        # Step 1: F_A claims point P
        store.upsert_points(
            COLLECTION, [make_point("shared_P", "src/file_a.py", VECTOR_A)]
        )

        # Step 2: F_B claims the SAME point_id P (different file, same logical chunk)
        store.upsert_points(
            COLLECTION, [make_point("shared_P", "src/file_b.py", VECTOR_B)]
        )

        # Step 3: F_A re-indexed without P — P is orphaned for F_A
        store.upsert_points(
            COLLECTION, [make_point("file_a_new_Q", "src/file_a.py", VECTOR_C)]
        )

        # Trigger HNSW incremental update — warning fires here if P is missing
        with caplog.at_level(logging.WARNING):
            store.end_indexing(COLLECTION)

        # P must be present in the persisted index (F_B still owns it)
        assert "shared_P" in store.load_id_index(COLLECTION), (
            "Bug #663: shared_P must remain in the persisted index after F_A orphans it, "
            "because F_B still owns it"
        )

        # No warning should reference shared_P
        warning_messages = [
            r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING and "shared_P" in r.getMessage()
        ]
        assert len(warning_messages) == 0, (
            f"Bug #663: No HNSW warning expected for shared_P, but got: {warning_messages}"
        )
