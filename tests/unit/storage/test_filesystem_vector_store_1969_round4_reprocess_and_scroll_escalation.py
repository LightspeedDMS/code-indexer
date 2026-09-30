"""Unit tests for Bug #1969 Round 4 (R3-F2, still current) and Round 5
(R4-F1, superseding R3-F1's storage-layer mechanism):

R3-F2 (P3): `scroll_points()`'s OWN Bug #1579 self-heal (a SEPARATE call
site from `IDIndexManager.rebuild_from_vectors()`, used by `--reconcile`)
must ALSO escalate `DedupRepairAmbiguousReason.CORRUPT_ID_INDEX` to the
same whole-file-wipe recovery -- otherwise `--reconcile` still hard-fails
on the exact scenario Round 3 was supposed to fix everywhere. (Unchanged
by Round 5.)

Round 5 (R4-F1): Round 4's in-memory `_pending_self_heal_reprocess_paths`
queue (drained by exactly ONE code path, lost on process exit) was
superseded by a DURABLE sidecar recorded inside collection_dedup_repair.
py's `recover_from_corrupt_id_index_by_wiping_files()` itself. This file's
`TestDurableSelfHealReprocessSidecarAccess` class replaces the old
`TestDrainPendingSelfHealReprocessPaths` (which tested the now-removed
`drain_pending_self_heal_reprocess_paths()`) -- it verifies
`get_pending_self_heal_reprocess_paths()` (read-only, does not clear) and
`clear_self_heal_reprocess_paths()` (explicit, called only after
successful reprocessing) against the real durable sidecar file.
"""

import hashlib
import json
import logging
from pathlib import Path

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
)

VECTOR_DIM = 4


def _write_repairable_duplicate_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    shard_suffix: str,
    path: str = "src/foo.py",
) -> Path:
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = hashlib.md5(unique_key.encode()).hexdigest()
    payload = {
        "path": path,
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": 1,
        "line_start": 1,
        "line_end": 10,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _add_hnsw_build_metadata(collection_dir: Path) -> None:
    meta_path = collection_dir / "collection_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["hnsw_index"] = {
        "version": 1,
        "vector_dim": VECTOR_DIM,
        "space": "cosine",
        "vector_count": 0,
        "id_mapping": {},
    }
    meta_path.write_text(json.dumps(meta))


def _corrupt_id_index_bin(collection_dir: Path) -> None:
    (collection_dir / "id_index.bin").write_bytes(b"\xff\xff\xff\xff")


def _build_corrupt_collection_with_repairable_duplicate(tmp_path: Path, path: str):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    _add_hnsw_build_metadata(collection_path)
    _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round4",
        index=0,
        vector=[0.1, 0.2, 0.3, 0.4],
        shard_suffix="-a",
        path=path,
    )
    _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round4",
        index=0,
        vector=[0.9, 0.9, 0.9, 0.9],
        shard_suffix="-b",
        path=path,
    )
    _corrupt_id_index_bin(collection_path)
    return store, collection_path


class TestDurableSelfHealReprocessSidecarAccess:
    """Round 5 (R4-F1): the durable sidecar (written by
    recover_from_corrupt_id_index_by_wiping_files() itself) replaces
    Round 4's in-memory queue. get_pending_self_heal_reprocess_paths()
    is read-only (repeatable, never auto-clears); clear_self_heal_
    reprocess_paths() is the explicit, separate clear step."""

    def test_load_id_index_self_heal_populates_durable_sidecar(
        self, tmp_path: Path
    ) -> None:
        store, _ = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round4_load.py"
        )

        store._load_id_index("coll", self_heal=True)

        pending = store.get_pending_self_heal_reprocess_paths("coll")
        assert pending == frozenset({"src/round4_load.py"})

    def test_get_is_repeatable_does_not_auto_clear(self, tmp_path: Path) -> None:
        store, _ = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round4_drain.py"
        )
        store._load_id_index("coll", self_heal=True)

        first = store.get_pending_self_heal_reprocess_paths("coll")
        assert first == frozenset({"src/round4_drain.py"})

        second = store.get_pending_self_heal_reprocess_paths("coll")
        assert second == frozenset({"src/round4_drain.py"})

    def test_clear_removes_the_entry(self, tmp_path: Path) -> None:
        store, _ = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round4_clear.py"
        )
        store._load_id_index("coll", self_heal=True)
        assert store.get_pending_self_heal_reprocess_paths("coll") == frozenset(
            {"src/round4_clear.py"}
        )

        store.clear_self_heal_reprocess_paths("coll", ["src/round4_clear.py"])

        assert store.get_pending_self_heal_reprocess_paths("coll") == frozenset()

    def test_no_self_heal_leaves_pending_reprocess_empty(self, tmp_path: Path) -> None:
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)

        assert store.get_pending_self_heal_reprocess_paths("coll") == frozenset()

    def test_unknown_collection_reads_empty(self, tmp_path: Path) -> None:
        store = FilesystemVectorStore(base_path=tmp_path)
        assert store.get_pending_self_heal_reprocess_paths("nonexistent") == frozenset()


class TestScrollPointsSelfHealEscalation:
    """R3-F2: scroll_points()'s own Bug #1579 self-heal call site must
    ALSO escalate CORRUPT_ID_INDEX -- this is a SEPARATE code path from
    IDIndexManager.rebuild_from_vectors() (Round 3's original fix)."""

    def test_scroll_points_now_self_heals_the_corrupt_index_case(
        self, tmp_path: Path, caplog
    ) -> None:
        store, collection_path = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round4_scroll.py"
        )

        with caplog.at_level(logging.WARNING):
            # Bug #1969 Round 6 (P1-1): the repair/wipe escalation this test
            # exercises is now gated on self_heal -- this test deliberately
            # opts in to prove the legitimate self-heal path still works.
            points, _next_offset = store.scroll_points(
                collection_name="coll", limit=100, self_heal=True
            )

        # Completes successfully -- no exception. The colliding file is
        # wholly wiped (whole-file-wipe recovery), so zero points remain
        # for it -- exactly what _get_indexed_files_snapshot needs to
        # treat it as needing reprocessing.
        assert isinstance(points, list)
        wiped_paths = {p.get("payload", {}).get("path") for p in points}
        assert "src/round4_scroll.py" not in wiped_paths
        remaining_vector_files = list(collection_path.rglob("vector_*.json"))
        assert remaining_vector_files == []

    def test_scroll_points_other_reason_still_propagates_unchanged(
        self, tmp_path: Path
    ) -> None:
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = tmp_path / "coll"
        _add_hnsw_build_metadata(collection_path)
        _write_repairable_duplicate_record(
            collection_path,
            project_id="proj",
            file_hash="sha256:round4other",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            shard_suffix="-a",
        )
        _write_repairable_duplicate_record(
            collection_path,
            project_id="proj",
            file_hash="sha256:round4other",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            shard_suffix="-b",
        )
        # A genuinely malformed record (missing 'id') -- ScrollDataIntegrityError
        # fires (either from this file directly, or from the duplicate
        # pair above), and repair_duplicate_and_shifted_points' own
        # pre-mutation malformed check fires with reason=MALFORMED_RECORDS,
        # never CORRUPT_ID_INDEX (id_index.bin is never even created in
        # this test) -- must propagate unescalated.
        (collection_path / "vector_malformed.json").write_text(
            json.dumps({"vector": [0.5]})
        )

        with pytest.raises(DedupRepairAmbiguousError):
            # Bug #1969 Round 6 (P1-1): self_heal=True so the dedup-repair
            # path actually runs and this test can prove a DIFFERENT
            # ambiguous reason (MALFORMED_RECORDS) still propagates
            # unescalated -- with self_heal=False (default) scroll_points
            # now raises the original ScrollDataIntegrityError before ever
            # calling repair_duplicate_and_shifted_points, which is a
            # different scenario covered separately in
            # test_filesystem_vector_store_1969_round6_scroll_self_heal_gate.py.
            store.scroll_points(collection_name="coll", limit=100, self_heal=True)
