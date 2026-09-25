"""Unit tests for Bug #1969 Round 3: the REAL corrupt-index self-heal,
wired into IDIndexManager.rebuild_from_vectors().

Round 2 made DuplicateSourceIdError self-healable via
repair_duplicate_and_shifted_points(), but that repair's own AC30 safety
rule (_plan_dedup) cannot resolve a per-chunk winner when id_index.bin
itself is corrupt -- it raises DedupRepairAmbiguousError(reason=
DedupRepairAmbiguousReason.CORRUPT_ID_INDEX) instead: a better-labeled
hard failure, not a silent recovery. This is the EXACT scenario from the
original bug report (corrupt/missing id_index.bin + a legacy duplicate
point_id).

This module proves rebuild_from_vectors() now escalates ONLY that one
specific reason to recover_from_corrupt_id_index_by_wiping_files()
(collection_dedup_repair.py), retries the scan exactly once more, and
that every other DedupRepairAmbiguousError reason still propagates
unchanged -- plus the bounded-retry discipline: a third failure of any
kind (from the escalated recovery itself) propagates, never loops.
"""

import hashlib
import json
import logging
from pathlib import Path

import pytest

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
)
import code_indexer.storage.shared.collection_dedup_repair as repair_mod


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _write_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    total_chunks: int,
    vector: list,
    line_start,
    line_end,
    shard_suffix: str,
    path: str = "src/foo.py",
) -> Path:
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = _point_id(project_id, file_hash, index)
    payload = {
        "path": path,
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": total_chunks,
        "line_start": line_start,
        "line_end": line_end,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(
    collection_dir: Path, *, vector_dim: int = 4, space: str = "cosine"
) -> None:
    meta = {
        "name": "coll",
        "vector_size": vector_dim,
        "hnsw_index": {
            "version": 1,
            "vector_dim": vector_dim,
            "space": space,
            "vector_count": 0,
            "id_mapping": {},
        },
    }
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _corrupt_id_index(collection_dir: Path) -> None:
    """Truncated id_index.bin -- fails to load entirely (the exact
    bug-report shape), which is what makes repair_duplicate_and_shifted_
    points' own _plan_dedup unable to resolve a winner."""
    (collection_dir / IDIndexManager.INDEX_FILENAME).write_bytes(b"\x05\x00\x00")


def _make_counting_wipe_recovery(monkeypatch):
    call_count = {"n": 0}
    original = repair_mod.recover_from_corrupt_id_index_by_wiping_files

    def _counting_wipe(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        repair_mod, "recover_from_corrupt_id_index_by_wiping_files", _counting_wipe
    )
    return call_count


class TestRealCorruptIndexSelfHealCompletesTheOriginalBugRepro:
    """The exact original bug-report reproduction: SHARDED_JSON collection
    with a corrupt/unreadable id_index.bin AND a real duplicate point_id
    in the genuine md5(unique_key) scheme."""

    def test_completes_and_wipes_both_colliding_files_entirely(
        self, tmp_path: Path, caplog
    ) -> None:
        _write_collection_meta(tmp_path)
        colliding_1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:corruptcase",
            index=1,
            total_chunks=2,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=10,
            line_end=19,
            shard_suffix="-a",
            path="src/collided.py",
        )
        colliding_2 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:corruptcase",
            index=1,
            total_chunks=3,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=10,
            line_end=25,
            shard_suffix="-b",
            path="src/collided.py",
        )
        # A completely unrelated, unaffected file -- proves the fix is
        # scoped, not a whole-collection wipe.
        unrelated = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:unrelated",
            index=0,
            total_chunks=1,
            vector=[0.5, 0.5, 0.5, 0.5],
            line_start=0,
            line_end=5,
            shard_suffix="-a",
            path="src/unrelated.py",
        )
        _corrupt_id_index(tmp_path)

        manager = IDIndexManager()
        with caplog.at_level(logging.WARNING):
            result = manager.rebuild_from_vectors(tmp_path, self_heal=True)

        # Completes successfully -- no exception.
        assert isinstance(result, dict)
        # Both colliding files' physical records are gone (all chunks of
        # that file_hash, not just the pair).
        assert not colliding_1.exists()
        assert not colliding_2.exists()
        assert unrelated.exists()

        # The returned id_index / on-disk id_index.bin has zero entries
        # for the affected file_hash.
        for point_id in result:
            assert "corruptcase" not in str(result[point_id])
        on_disk_index = IDIndexManager().load_index(tmp_path)
        assert len(on_disk_index) == 1
        assert next(iter(on_disk_index.values())).resolve() == unrelated.resolve()

        assert "Round 3" in caplog.text or "corrupt" in caplog.text.lower()

    def test_reconcile_safety_zero_points_for_affected_file(
        self, tmp_path: Path
    ) -> None:
        """Proves the post-condition smart_indexer.py's reconcile needs:
        after recovery, a fresh scan of the collection returns ZERO
        points with payload.path == the affected file -- so
        _get_indexed_files_snapshot correctly treats the file as fully
        MISSING (not half-indexed) and fully reprocesses it. A
        half-deleted file with an unchanged blob hash would otherwise
        never be reprocessed (this project's reconcile-safety
        constraint)."""
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:reconcilecheck",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/reconcile_me.py",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:reconcilecheck",
            index=0,
            total_chunks=1,
            vector=[0.8, 0.8, 0.8, 0.8],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
            path="src/reconcile_me.py",
        )
        _corrupt_id_index(tmp_path)

        manager = IDIndexManager()
        manager.rebuild_from_vectors(tmp_path, self_heal=True)

        remaining_records_for_file = [
            json.loads(p.read_text())
            for p in tmp_path.rglob("vector_*.json")
            if json.loads(p.read_text()).get("payload", {}).get("path")
            == "src/reconcile_me.py"
        ]
        assert remaining_records_for_file == [], (
            "smart_indexer.py's reconcile needs ZERO points for this "
            "path to correctly classify the file as fully missing and "
            "reprocess it -- any surviving point here would create a "
            "silent, permanent half-indexed state"
        )


class TestOnlyCorruptIndexReasonEscalates:
    """Any OTHER DedupRepairAmbiguousError reason (e.g. a malformed
    record present) must NOT trigger the new escalation and must
    propagate immediately, unchanged from round 2's behavior."""

    def test_malformed_record_reason_propagates_without_escalation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        colliding_1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:malformedcase",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
        )
        colliding_2 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:malformedcase",
            index=0,
            total_chunks=1,
            vector=[0.8, 0.8, 0.8, 0.8],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
        )
        # A genuinely malformed record (missing 'id') elsewhere -- the
        # repair's pre-mutation malformed check fires BEFORE _plan_dedup
        # is ever reached, so reason == MALFORMED_RECORDS, never
        # CORRUPT_ID_INDEX -- even though id_index.bin is ALSO corrupt.
        (tmp_path / "vector_malformed.json").write_text(json.dumps({"vector": [0.5]}))
        _corrupt_id_index(tmp_path)
        wipe_call_count = _make_counting_wipe_recovery(monkeypatch)

        manager = IDIndexManager()
        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            manager.rebuild_from_vectors(tmp_path, self_heal=True)

        from code_indexer.storage.shared.collection_dedup_repair import (
            DedupRepairAmbiguousReason,
        )

        assert exc_info.value.reason == DedupRepairAmbiguousReason.MALFORMED_RECORDS
        assert wipe_call_count["n"] == 0, (
            "the whole-file-wipe escalation must NEVER be invoked for a "
            "reason other than CORRUPT_ID_INDEX"
        )
        # Zero mutation -- collection left exactly as it was.
        assert colliding_1.exists()
        assert colliding_2.exists()


class TestBoundedRetryNeverLoopsEvenAcrossEscalation:
    """If the escalated recovery itself still leaves an unresolvable
    state (here: the HNSW build parameters cannot be determined during
    the escalation's own rebuild step), the failure propagates rather
    than looping -- the self-heal (including its escalation) is attempted
    EXACTLY ONCE per rebuild_from_vectors() call."""

    def test_third_failure_from_escalation_itself_propagates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Deliberately NO collection_meta.json -- the escalated wipe
        # recovery's own _resolve_hnsw_build_params call cannot determine
        # the HNSW build parameters, so it raises DedupRepairAmbiguousError
        # a second time (this time reason=HNSW_PARAMS_META_UNREADABLE).
        colliding_1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:thirdfailure",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
        )
        colliding_2 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:thirdfailure",
            index=0,
            total_chunks=1,
            vector=[0.8, 0.8, 0.8, 0.8],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
        )
        _corrupt_id_index(tmp_path)
        wipe_call_count = _make_counting_wipe_recovery(monkeypatch)
        repair_call_count = {"n": 0}
        original_repair = repair_mod.repair_duplicate_and_shifted_points

        def _counting_repair(*args, **kwargs):
            repair_call_count["n"] += 1
            return original_repair(*args, **kwargs)

        monkeypatch.setattr(
            repair_mod, "repair_duplicate_and_shifted_points", _counting_repair
        )

        manager = IDIndexManager()
        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            manager.rebuild_from_vectors(tmp_path, self_heal=True)

        from code_indexer.storage.shared.collection_dedup_repair import (
            DedupRepairAmbiguousReason,
        )

        assert (
            exc_info.value.reason
            == DedupRepairAmbiguousReason.HNSW_PARAMS_META_UNREADABLE
        )
        assert repair_call_count["n"] == 1, (
            "the normal repair must be attempted exactly once"
        )
        assert wipe_call_count["n"] == 1, (
            "the escalation must be attempted exactly once -- its own "
            "failure must propagate, never trigger a second escalation "
            "attempt (bounded, no infinite loop)"
        )
        # Zero mutation: the escalation itself resolves HNSW params
        # before deleting anything (see collection_dedup_repair.py's own
        # ordering fix), so both colliding records survive untouched.
        assert colliding_1.exists()
        assert colliding_2.exists()
