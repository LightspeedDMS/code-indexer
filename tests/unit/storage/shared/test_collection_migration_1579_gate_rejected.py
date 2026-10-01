"""Bug #1579 Part 3 tests: consolidate_collection_in_place() must return a
distinct, honest "dedup_gate_rejected" status -- instead of crashing with
DuplicateSourceIdError -- when repair_duplicate_and_shifted_points's
whole-collection identity gate rejects a collection that GENUINELY has
duplicate point_id group(s) present (a foreign/missing/self-inconsistent
unique_key was found on SOME OTHER, unrelated record elsewhere in the same
collection).

Before this fix: DedupRepairResult.duplicate_groups was never populated on
the gate_passed=False return path, so consolidate_collection_in_place's
"if not repair_result.gate_passed:" branch could not distinguish "gate
rejected, no duplicates anyway" (safe to fall through to the normal scan)
from "gate rejected AND duplicates are genuinely present" (falling through
crashes Step 1's _scan_or_fail_on_rejected_records with
DuplicateSourceIdError, an uncaught, unhelpful failure for a genuinely
dangerous state).

All tests use REAL files and REAL filesystem operations -- no mocking of
consolidate_collection_in_place or repair_duplicate_and_shifted_points.
"""

from pathlib import Path

from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)
from tests.unit.storage.shared.test_collection_dedup_repair_1502 import (
    _write_collection_meta,
    _write_record,
)


def _snapshot_vector_files(collection_dir: Path) -> dict:
    """Map of relative path -> file bytes, for every raw vector_*.json
    record. Used to prove a rejected consolidation left the collection
    byte-for-byte untouched (not merely "same file paths")."""
    return {
        str(f.relative_to(collection_dir)): f.read_bytes()
        for f in sorted(collection_dir.rglob("vector_*.json"))
    }


def _write_duplicate_pair(tmp_path: Path) -> None:
    """A genuine, natural duplicate: same (project_id, file_hash, index)
    computes the SAME point_id via md5(unique_key) -- self-consistent with
    the identity scheme in isolation (the gate rejection below comes from a
    SEPARATE, unrelated record)."""
    _write_record(
        tmp_path,
        project_id="proj",
        file_hash="sha256:dup",
        index=1,
        vector=[0.1, 0.2, 0.3, 0.4],
        line_start=1,
        line_end=10,
        shard_suffix="-a",
    )
    _write_record(
        tmp_path,
        project_id="proj",
        file_hash="sha256:dup",
        index=1,
        vector=[0.9, 0.9, 0.9, 0.9],
        line_start=1,
        line_end=10,
        shard_suffix="-b",
    )


def _write_gate_breaking_record(tmp_path: Path) -> None:
    """Unrelated record with no unique_key -- breaks the WHOLE-collection
    identity gate (the gate's scope is the entire collection, not just this
    record's own group)."""
    _write_record(
        tmp_path,
        project_id="proj",
        file_hash="sha256:foreign",
        index=0,
        vector=[0.5, 0.5, 0.5, 0.5],
        line_start=1,
        line_end=5,
        omit_unique_key=True,
    )


class TestConsolidateCollectionInPlaceGateRejectedWithDuplicates:
    def test_gate_rejected_with_genuine_duplicates_reports_distinct_status(
        self, tmp_path: Path
    ) -> None:
        """Pre-fix: consolidate_collection_in_place crashes with
        DuplicateSourceIdError. Post-fix: it returns
        ConsolidationResult(status="dedup_gate_rejected", deletion_gated=
        False) and mutates NOTHING on disk.
        """
        _write_collection_meta(tmp_path, vector_dim=4)
        _write_duplicate_pair(tmp_path)
        _write_gate_breaking_record(tmp_path)

        snapshot_before = _snapshot_vector_files(tmp_path)
        assert len(snapshot_before) == 3, "sanity: fixture has 3 raw records"

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "dedup_gate_rejected"
        assert result.deletion_gated is False, (
            "dedup_gate_rejected is a DIFFERENT gate than Story #1460's "
            "rollout-safety deletion_authorized gate"
        )
        assert _snapshot_vector_files(tmp_path) == snapshot_before, (
            "repair must pass the collection through UNTOUCHED -- zero "
            "files deleted, added, or content-mutated"
        )
        assert not (tmp_path / "chunks.db").exists(), (
            "no chunks.db should be written when consolidation is rejected"
        )

    def test_gate_rejected_with_zero_duplicates_still_consolidates_normally(
        self, tmp_path: Path
    ) -> None:
        """Regression: the PRE-EXISTING 'gate rejected but zero actual
        duplicates' case (a legacy-schema collection with a foreign/missing
        unique_key but no duplicate point_id anywhere) must still fall
        through to a normal, successful consolidation -- exactly as it did
        before this fix.
        """
        _write_collection_meta(tmp_path, vector_dim=4)
        _write_gate_breaking_record(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
