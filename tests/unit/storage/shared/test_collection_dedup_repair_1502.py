"""Unit tests for collection_dedup_repair.py (Bug #1502, remediated per dual
code review -- Claude code-reviewer + Codex, including the Codex delta
re-review round).

Metadata-only dedup + renumber repair for legacy SHARDED_JSON collections
carrying duplicate point_ids and/or shifted chunk labels, caused by the
enumerate()-over-survivors bug fixed in file_chunking_manager.py.

Remediation summary (round 1):
  - Rebuild-derived-artifacts: id_index.bin and the HNSW index + id_mapping
    are REBUILT from the repaired JSON tree (IDIndexManager.
    rebuild_from_vectors / HNSWIndexManager.rebuild_from_vectors) instead
    of a bespoke forward-map, gated by a durable crash marker.
  - Whole-collection identity gate (H5+F2): repair proceeds only if EVERY
    scanned record has a parseable, self-consistent (md5(unique_key)==id)
    unique_key; otherwise the whole collection passes through untouched.
  - Malformed-record pre-check (H4): any unreadable/malformed vector JSON
    fails the whole repair loudly BEFORE any mutation.
  - Real gap-continuity check (F1), tolerance derived from actual
    FixedSizeChunker output.

Remediation summary (Codex delta re-review round):
  - Hidden-branch filtering (Bug #306) is preserved across the rebuild by
    threading the collection's recorded current_branch through to
    HNSWIndexManager.rebuild_from_vectors.
  - An empty post-repair JSON tree combined with a stale crash marker is
    treated as anomalous and fails loud (never silently "converges" over
    a potentially-stale HNSW index).
  - Invalid UTF-8 (and any other read failure) is classified as a
    malformed record, closing a gap that let it escape the pre-mutation
    check.
  - HNSW build parameters (vector dimension, distance metric) are read
    ONLY from collection_meta.json as the authoritative source and fail
    loud, pre-mutation, when undeterminable -- never silently defaulted.

All tests use REAL files and REAL filesystem operations -- no mocking of
the module under test (monkeypatch is used only to inject crash points at
exact phase boundaries, per Codex's test-quality finding).
"""

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.config import IndexingConfig
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.id_index_manager import DuplicateSourceIdError, IDIndexManager
import code_indexer.storage.shared.collection_dedup_repair as repair_mod
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
    parse_unique_key,
    repair_duplicate_and_shifted_points,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _write_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    line_start: Optional[int],
    line_end: Optional[int],
    point_id_override: Optional[str] = None,
    unique_key_override: Optional[str] = None,
    omit_unique_key: bool = False,
    extra_payload: Optional[Dict[str, Any]] = None,
    shard_suffix: str = "",
) -> Path:
    """Write one legacy sharded vector_<id>.json record mirroring the real
    production payload shape (unique_key/point_id/chunk_index/total_chunks
    all present, matching GitAwareMetadataSchema + _create_vector_point).

    By default the point_id is the NATURAL md5(unique_key) formula -- two
    calls with the SAME (project_id, file_hash, index) therefore collide
    on id exactly like the real bug (same label, different content),
    while staying self-consistent with the whole-collection identity
    gate. point_id_override/unique_key_override are for deliberately
    constructing gate-violating or foreign-format fixtures.
    """
    unique_key = unique_key_override or f"{project_id}_{file_hash}_{index}"
    point_id = point_id_override or _point_id(project_id, file_hash, index)

    payload: Dict[str, Any] = {
        "path": "src/foo.py",
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": 1,
        "line_start": line_start,
        "line_end": line_end,
        "point_id": point_id,
    }
    if not omit_unique_key:
        payload["unique_key"] = unique_key
    if extra_payload:
        payload.update(extra_payload)
    record = {"id": point_id, "vector": vector, "payload": payload}

    # Different shard subdirectory per call (simulates two indexing runs
    # placing a duplicate point_id under DIFFERENT quantized-vector shard
    # directories, per issue #1502's confirmed root cause).
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(
    collection_dir: Path,
    id_mapping: Optional[Dict[str, str]] = None,
    *,
    vector_dim: int = 4,
    space: str = "cosine",
    current_branch: Optional[str] = None,
    omit_hnsw_metadata: bool = False,
) -> None:
    """Real production-shaped collection_meta.json: hnsw_index.vector_dim
    and .space are ALWAYS present by default (Codex finding 5 requires
    these as the sole authoritative source) -- matching a collection
    that has genuinely been through at least one real HNSW build, which
    is the only kind of collection this repair ever legitimately
    operates on. omit_hnsw_metadata=True simulates the anomalous case
    finding 5's fail-loud test targets."""
    meta: Dict[str, Any] = {"name": "coll", "vector_size": vector_dim}
    if not omit_hnsw_metadata:
        hnsw_index: Dict[str, Any] = {
            "version": 1,
            "vector_dim": vector_dim,
            "space": space,
            "vector_count": len(id_mapping) if id_mapping is not None else 0,
            "id_mapping": id_mapping or {},
        }
        if current_branch is not None:
            hnsw_index["current_branch"] = current_branch
        meta["hnsw_index"] = hnsw_index
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _all_json_files(collection_dir: Path) -> set:
    return {
        p for p in collection_dir.rglob("*.json") if "collection_meta" not in p.name
    }


def _marker_path(collection_dir: Path) -> Path:
    return collection_dir / ".dedup-repair-pending"


def _current_json_tree_ids(collection_dir: Path) -> set:
    ids = set()
    for jf in collection_dir.rglob("vector_*.json"):
        data = json.loads(jf.read_text())
        ids.add(data["id"])
    return ids


def _assert_derived_artifacts_consistent(collection_dir: Path) -> None:
    """Load id_index.bin and collection_meta.json's HNSW id_mapping and
    assert every value in each derived artifact exists in the CURRENT
    JSON tree's point_id set, and vice versa (Codex crash-window test
    requirement (c))."""
    json_ids = _current_json_tree_ids(collection_dir)

    id_index = IDIndexManager().load_index(collection_dir)
    assert set(id_index.keys()) == json_ids, (
        f"id_index.bin keys {set(id_index.keys())} != JSON tree ids {json_ids}"
    )

    meta = json.loads((collection_dir / "collection_meta.json").read_text())
    id_mapping = meta.get("hnsw_index", {}).get("id_mapping", {})
    mapping_ids = set(id_mapping.values())
    assert mapping_ids == json_ids, (
        f"id_mapping values {mapping_ids} != JSON tree ids {json_ids}"
    )


def _assert_hnsw_labels_resolve_to_correct_vectors(
    collection_dir: Path, vector_dim: int = 4, space: str = "cosine"
) -> None:
    """Codex finding 7f: prove the REBUILT HNSW index's labels actually
    resolve to the CORRECT point_id's vector by loading the real
    hnsw_index.bin and querying it directly -- not merely that the
    id_mapping's value SET matches the JSON tree (which could pass even
    with a wrong label<->id assignment)."""
    meta = json.loads((collection_dir / "collection_meta.json").read_text())
    hnsw_meta = meta["hnsw_index"]
    id_mapping = hnsw_meta["id_mapping"]

    vectors_by_id: Dict[str, List[float]] = {}
    for jf in collection_dir.rglob("vector_*.json"):
        data = json.loads(jf.read_text())
        vectors_by_id[data["id"]] = data["vector"]

    index = HNSWIndexManager(vector_dim=vector_dim, space=space).load_index(
        collection_dir
    )
    assert index is not None, "hnsw_index.bin missing after rebuild"

    for label_str, point_id in id_mapping.items():
        label = int(label_str)
        [stored_vector] = index.get_items([label])
        expected_vector = vectors_by_id[point_id]
        if space == "cosine":
            # hnswlib's cosine space internally L2-normalizes stored
            # vectors -- normalize the expected vector the SAME way
            # before comparing (this is genuine hnswlib behavior, not a
            # production bug).
            norm = sum(v * v for v in expected_vector) ** 0.5
            expected_vector = [v / norm for v in expected_vector]
        for actual, expected in zip(stored_vector, expected_vector):
            assert abs(actual - expected) < 1e-4, (
                f"label {label} resolves to point_id {point_id!r} but its "
                f"stored HNSW vector {list(stored_vector)} does not match "
                f"the JSON tree's vector {expected_vector} for that id"
            )

    assert hnsw_meta["vector_count"] == len(id_mapping) == len(vectors_by_id)


class TestParseUniqueKey:
    def test_parses_simple_project_id(self) -> None:
        project_id, file_hash, index = parse_unique_key("evolution_sha256:4d2513c2_62")
        assert project_id == "evolution"
        assert file_hash == "sha256:4d2513c2"
        assert index == 62

    def test_parses_project_id_containing_underscores(self) -> None:
        project_id, file_hash, index = parse_unique_key(
            "my_cool_project_sha256:deadbeef1234_7"
        )
        assert project_id == "my_cool_project"
        assert file_hash == "sha256:deadbeef1234"
        assert index == 7

    def test_rejects_missing_sha256_anchor(self) -> None:
        with pytest.raises(ValueError):
            parse_unique_key("project_nothash_3")

    def test_rejects_non_integer_index(self) -> None:
        with pytest.raises(ValueError):
            parse_unique_key("project_sha256:abc_notanumber")

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValueError):
            parse_unique_key("")

    def test_rejects_none(self) -> None:
        with pytest.raises(ValueError):
            parse_unique_key(None)


class TestFixedSizeChunkerOverlapDerivesRenumberTolerance:
    """Claude F1: the gap-continuity tolerance used in _plan_renumber must
    be DERIVED from the real chunker's behavior, never guessed. This test
    proves the invariant against genuine FixedSizeChunker output."""

    def test_real_chunker_output_satisfies_line_start_le_line_end_plus_one(
        self,
    ) -> None:
        chunker = FixedSizeChunker(IndexingConfig())
        # Enough distinct short lines to comfortably exceed the default
        # 1000-char chunk_size several times over (multi-chunk, real
        # 15%-overlap arithmetic).
        lines = [f"line number {i:04d} of the test content" for i in range(400)]
        text = "\n".join(lines)

        chunks = chunker.chunk_text(text)

        assert len(chunks) >= 5, "fixture must produce a genuine multi-chunk file"
        for i in range(len(chunks) - 1):
            assert chunks[i + 1]["line_start"] <= chunks[i]["line_end"] + 1, (
                f"real chunker output violated the derived continuity "
                f"tolerance between chunk {i} (line_end="
                f"{chunks[i]['line_end']}) and chunk {i + 1} (line_start="
                f"{chunks[i + 1]['line_start']})"
            )


class TestRepairNoDuplicates:
    def test_no_op_when_labels_already_canonical(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:aaa",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:aaa",
            index=1,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
        )
        before = _all_json_files(tmp_path)

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.duplicates_found == 0
        assert result.gate_passed is True
        # Identity fast path: nothing changed, no marker existed -- zero
        # mutation, zero rebuild.
        assert result.id_index_rebuilt is False
        assert result.hnsw_rebuilt is False
        after = _all_json_files(tmp_path)
        assert before == after
        assert not _marker_path(tmp_path).exists()

    def test_renumbers_shifted_labels_on_non_dup_file(self, tmp_path: Path) -> None:
        """Simulates issue #1502's confirmed real-data shape: labels don't
        match line order (shifted, no duplicate id involved)."""
        _write_collection_meta(tmp_path)
        # Old (wrong) index=5 actually holds the FIRST chunk by line order,
        # old index=6 holds the SECOND. Neither collides (no duplicate).
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:bbb",
            index=5,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:bbb",
            index=6,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
        )

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.duplicates_found == 0
        assert result.records_renumbered == 2
        assert result.id_index_rebuilt is True
        assert result.hnsw_rebuilt is True
        assert not _marker_path(tmp_path).exists()

        expected_id_0 = _point_id("proj", "sha256:bbb", 0)
        expected_id_1 = _point_id("proj", "sha256:bbb", 1)
        found_ids = set()
        for jf in _all_json_files(tmp_path):
            data = json.loads(jf.read_text())
            found_ids.add(data["id"])
            assert data["payload"]["total_chunks"] == 2
        assert found_ids == {expected_id_0, expected_id_1}
        _assert_derived_artifacts_consistent(tmp_path)
        # Codex 7f: exact content, not just a value-set match.
        _assert_hnsw_labels_resolve_to_correct_vectors(tmp_path)


class TestRepairDeduplication:
    def test_duplicate_resolved_via_id_index_winner_loser_quarantined(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        # Natural collision: SAME (project_id, file_hash, index) computes
        # the SAME point_id via the real formula -- exactly how the
        # confirmed real bug forges a duplicate (two runs, same label,
        # different content), and self-consistent with the identity gate.
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ccc",
            index=7,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=100,
            line_end=110,
            shard_suffix="-a",
        )
        loser_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ccc",
            index=7,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=100,
            line_end=110,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:ccc", 7)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.duplicates_found == 1
        assert result.duplicates_quarantined == 1
        assert result.id_index_rebuilt is True
        assert result.hnsw_rebuilt is True
        assert not _marker_path(tmp_path).exists()

        # Loser physically removed from the collection tree.
        assert not loser_path.exists()
        # Winner survives inside the collection tree.
        remaining = _all_json_files(tmp_path)
        assert len(remaining) == 1
        surviving_path = next(iter(remaining))
        assert tmp_path in surviving_path.parents

        surviving_data = json.loads(surviving_path.read_text())
        # Winner's vector [0.1,...] survives, under the new canonical id
        # (line_start=100 is this file's ONLY surviving chunk -> index 0).
        assert surviving_data["vector"] == [0.1, 0.2, 0.3, 0.4]
        assert surviving_data["payload"]["chunk_index"] == 0
        _assert_derived_artifacts_consistent(tmp_path)
        # Codex 7f: prove the HNSW label for the surviving id resolves to
        # the WINNER's vector, never the quarantined loser's.
        _assert_hnsw_labels_resolve_to_correct_vectors(tmp_path)

    def test_quarantined_file_invisible_to_downstream_json_scans(
        self, tmp_path: Path
    ) -> None:
        """A quarantined loser must never satisfy a bare
        collection_dir.rglob('vector_*.json') scan -- the exact scan
        consolidate_collection_in_place's own IDIndexManager relies on."""
        _write_collection_meta(tmp_path)
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ddd",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ddd",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:ddd", 0)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        repair_duplicate_and_shifted_points(tmp_path)

        # Re-running the exact scan consolidate_collection_in_place uses
        # afterward must see exactly ONE record, no duplicates, no error.
        id_map, rejected_count = IDIndexManager().scan_vectors_for_id_map_verbose(
            tmp_path
        )
        assert rejected_count == 0
        assert len(id_map) == 1
        assert len(list(tmp_path.rglob("vector_*.json"))) == 1

    def test_quarantine_fsyncs_source_and_destination_directories(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex M7: after moving a loser to the quarantine sidecar, both
        the SOURCE directory (loser's original shard dir) and the
        DESTINATION directory (quarantine sidecar dir) must be fsynced."""
        _write_collection_meta(tmp_path)
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:m7m",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        loser_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:m7m",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:m7m", 0)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        fsynced_dirs: List[str] = []
        real_fsync = repair_mod.nfs_safe_fsync
        real_fdopendir_source = loser_path.parent

        def _tracking_fsync(fd: int) -> None:
            # Resolve fd back to its directory path via /proc for assertion.
            import os as _os

            try:
                dir_path = _os.readlink(f"/proc/self/fd/{fd}")
                fsynced_dirs.append(dir_path)
            except OSError:
                pass
            real_fsync(fd)

        monkeypatch.setattr(repair_mod, "nfs_safe_fsync", _tracking_fsync)

        repair_duplicate_and_shifted_points(tmp_path)

        # The loser's original shard directory and SOME quarantine
        # destination directory must both have been fsynced.
        assert any(str(real_fdopendir_source.resolve()) == d for d in fsynced_dirs), (
            f"source shard dir never fsynced; fsynced dirs were {fsynced_dirs}"
        )
        assert any(".dedup-quarantine" in d for d in fsynced_dirs), (
            f"quarantine destination dir never fsynced; fsynced dirs were {fsynced_dirs}"
        )


class TestRepairFailLoudDedupAmbiguity:
    def test_duplicate_with_no_id_index_entry_raises_and_leaves_untouched(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:eee",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:eee",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        # No id_index.bin at all -- no winner reference available.
        before = {p: p.read_bytes() for p in _all_json_files(tmp_path)}

        with pytest.raises(DuplicateSourceIdError):
            repair_duplicate_and_shifted_points(tmp_path)

        after = {p: p.read_bytes() for p in _all_json_files(tmp_path)}
        assert before == after
        assert not _marker_path(tmp_path).exists()

    def test_id_index_referencing_neither_copy_raises_and_leaves_untouched(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:fff",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:fff",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:fff", 0)
        bogus_path = tmp_path / "does" / "not" / "exist.json"
        IDIndexManager().save_index(tmp_path, {shared_point_id: bogus_path})
        before = {p: p.read_bytes() for p in _all_json_files(tmp_path)}

        with pytest.raises(DuplicateSourceIdError):
            repair_duplicate_and_shifted_points(tmp_path)

        after = {p: p.read_bytes() for p in _all_json_files(tmp_path)}
        assert before == after
        assert not _marker_path(tmp_path).exists()


class TestRepairGapContinuity:
    """Claude F1, AMENDED per live-staging E2E on the real evolution repo
    (Bug #1502 follow-up): a genuine LINE GAP between consecutive chunks
    in a file group (exceeding the real-chunker-derived tolerance proven
    above), or two distinct records sharing an identical line range, no
    longer refuses the WHOLE collection. Census across the real
    evolution-repo collection found 586 of 10,579 file groups (5.5%)
    carry genuine historical line gaps (chunks silently dropped by the
    pre-fix code) -- whole-collection refusal on ANY such group would
    make migration permanently impossible for that repo, and realistically
    every long-lived production repo. Instead, the offending group alone
    is EXCLUDED from the renumber plan (its records keep their existing
    point_ids/labels, byte-identical) while the rest of the collection
    (dedup, and renumbering of every OTHER, non-violating group) proceeds
    normally, with a single summary WARNING documenting the skip."""

    def test_genuine_line_gap_skips_group_but_leaves_it_byte_identical(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_collection_meta(tmp_path)
        rec0 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gap",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        # A real gap: chunk 2 starts at line 500, far beyond chunk 1's
        # line_end(10) + 1 -- no real chunker overlap could produce this.
        rec1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gap",
            index=1,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=500,
            line_end=510,
        )
        before = {rec0: rec0.read_bytes(), rec1: rec1.read_bytes()}

        with caplog.at_level(logging.WARNING, logger=repair_mod.__name__):
            result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.groups_skipped_renumber == 1
        assert result.skipped_renumber_file_hashes == ["sha256:gap"]
        assert result.records_renumbered == 0
        # The gapped group's records are byte-identical -- untouched.
        assert rec0.read_bytes() == before[rec0]
        assert rec1.read_bytes() == before[rec1]
        # Nothing else changed anywhere else in the collection either --
        # no mutation at all was required, so no marker was ever written.
        assert not _marker_path(tmp_path).exists()
        assert any("skipped renumbering" in r.message for r in caplog.records), (
            "must emit exactly one WARNING summarizing the skipped group(s)"
        )

    def test_non_contiguous_duplicate_line_range_skips_group_but_leaves_it_byte_identical(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two DISTINCT (non-duplicate-id) records in the same file group
        sharing the exact same (line_start, line_end) range get the SAME
        per-group graceful degradation as a genuine line gap -- not
        whole-collection refusal."""
        _write_collection_meta(tmp_path)
        rec0 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:hhh",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        rec1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:hhh",
            index=1,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=1,
            line_end=10,
        )
        before = {rec0: rec0.read_bytes(), rec1: rec1.read_bytes()}

        with caplog.at_level(logging.WARNING, logger=repair_mod.__name__):
            result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.groups_skipped_renumber == 1
        assert result.skipped_renumber_file_hashes == ["sha256:hhh"]
        assert result.records_renumbered == 0
        assert rec0.read_bytes() == before[rec0]
        assert rec1.read_bytes() == before[rec1]
        assert not _marker_path(tmp_path).exists()
        assert any("skipped renumbering" in r.message for r in caplog.records)


class TestPerGroupRenumberGracefulDegradation:
    """Bug #1502 live-staging amendment, end-to-end: a collection with a
    MIX of a genuinely-gapped group, a clean shifted-label group, and a
    dedup-needing group must repair the clean/dedup groups normally,
    leave the gapped group byte-identical, and still consolidate
    end-to-end -- every group's content correctly present in chunks.db
    afterward."""

    def test_mixed_collection_dedups_and_renumbers_clean_groups_skips_gapped_group(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)

        # Group A ("gapA"): genuine line gap -- must be skipped entirely,
        # byte-identical, keeping its OLD (non-canonical) point_ids.
        gap0 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gapA",
            index=0,
            vector=[0.1, 0.1, 0.1, 0.1],
            line_start=1,
            line_end=10,
        )
        gap1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gapA",
            index=1,
            vector=[0.2, 0.2, 0.2, 0.2],
            line_start=500,
            line_end=510,
        )
        gap_before = {gap0: gap0.read_bytes(), gap1: gap1.read_bytes()}

        # Group B ("cleanB"): shifted labels, no gap, no duplicate --
        # must be renumbered canonically (5,6 -> 0,1).
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:cleanB",
            index=5,
            vector=[0.3, 0.3, 0.3, 0.3],
            line_start=1,
            line_end=10,
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:cleanB",
            index=6,
            vector=[0.4, 0.4, 0.4, 0.4],
            line_start=11,
            line_end=20,
        )

        # Group C ("dupC"): one duplicate point_id -- winner resolved via
        # id_index.bin, loser quarantined; the surviving winner (the
        # file's ONLY chunk) is then renumbered to its canonical index 0.
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:dupC",
            index=7,
            vector=[0.5, 0.5, 0.5, 0.5],
            line_start=100,
            line_end=110,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:dupC",
            index=7,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=100,
            line_end=110,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:dupC", 7)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        # ---- Repair-level assertions (direct call, before consolidation
        # deletes the legacy JSON files) ----
        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.duplicates_found == 1
        assert result.duplicates_quarantined == 1
        assert result.groups_skipped_renumber == 1
        assert result.skipped_renumber_file_hashes == ["sha256:gapA"]
        # cleanB's 2 records + dupC's 1 surviving winner all get a new
        # canonical id (5->0, 6->1, 7->0 respectively).
        assert result.records_renumbered == 3
        assert gap0.read_bytes() == gap_before[gap0]
        assert gap1.read_bytes() == gap_before[gap1]

        # ---- Consolidation-level assertion: end-to-end success, every
        # group's content correctly present in chunks.db ----
        consolidation_result = consolidate_collection_in_place(tmp_path)
        assert consolidation_result.status == "consolidated"

        expected_gap_id_0 = _point_id("proj", "sha256:gapA", 0)
        expected_gap_id_1 = _point_id("proj", "sha256:gapA", 1)
        expected_clean_id_0 = _point_id("proj", "sha256:cleanB", 0)
        expected_clean_id_1 = _point_id("proj", "sha256:cleanB", 1)
        expected_dup_winner_id = _point_id("proj", "sha256:dupC", 0)

        with ChunkStore(tmp_path / "chunks.db") as store:
            all_ids = set(store.all_point_ids())
            assert all_ids == {
                expected_gap_id_0,
                expected_gap_id_1,
                expected_clean_id_0,
                expected_clean_id_1,
                expected_dup_winner_id,
            }
            # The dedup winner's content (vector [0.5,...]) survives --
            # never the quarantined loser's [0.9,...].
            winner_record = store.read(expected_dup_winner_id)
            assert list(winner_record["vector"]) == [0.5, 0.5, 0.5, 0.5]

    def test_second_repair_run_on_mixed_collection_is_a_clean_no_op(
        self, tmp_path: Path
    ) -> None:
        """Idempotency: after a first run has resolved dedup/renumbered
        the clean group, a SECOND run must be a pure identity transform
        -- the gapped group is skipped again (still non-canonical), and
        NOTHING else changes (no churn, no rebuild)."""
        _write_collection_meta(tmp_path)
        gap0 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gapIdem",
            index=0,
            vector=[0.1, 0.1, 0.1, 0.1],
            line_start=1,
            line_end=10,
        )
        gap1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gapIdem",
            index=1,
            vector=[0.2, 0.2, 0.2, 0.2],
            line_start=500,
            line_end=510,
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:cleanIdem",
            index=5,
            vector=[0.3, 0.3, 0.3, 0.3],
            line_start=1,
            line_end=10,
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:cleanIdem",
            index=6,
            vector=[0.4, 0.4, 0.4, 0.4],
            line_start=11,
            line_end=20,
        )

        first = repair_duplicate_and_shifted_points(tmp_path)
        assert first.groups_skipped_renumber == 1
        assert first.records_renumbered == 2
        assert gap0.exists() and gap1.exists()

        before_second = {p: p.read_bytes() for p in _all_json_files(tmp_path)}

        second = repair_duplicate_and_shifted_points(tmp_path)

        assert second.duplicates_found == 0
        assert second.records_renumbered == 0
        assert second.groups_skipped_renumber == 1
        assert second.skipped_renumber_file_hashes == ["sha256:gapIdem"]
        # Fully clean identity pass: no marker existed, nothing changed,
        # so the fast path never touched id_index.bin/HNSW again either.
        assert second.id_index_rebuilt is False
        assert second.hnsw_rebuilt is False
        assert not _marker_path(tmp_path).exists()

        after_second = {p: p.read_bytes() for p in _all_json_files(tmp_path)}
        assert before_second == after_second, "second run must produce zero churn"


class TestMalformedRecordPreCheck:
    """Codex H4: a malformed vector JSON anywhere in the collection must
    fail the WHOLE repair loudly BEFORE any mutation of any OTHER record
    -- never mutate first and discover the malformed record later."""

    def test_malformed_record_alongside_valid_shifted_record_raises_untouched(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        # A perfectly valid, shifted-label record that WOULD normally be
        # renumbered if it were the only issue in the collection.
        valid_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:h4h",
            index=5,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        # A genuinely malformed vector record: unreadable JSON.
        malformed_path = (
            tmp_path / "cc" / "dd" / "vector_deadbeef00000000000000000000000.json"
        )
        malformed_path.parent.mkdir(parents=True, exist_ok=True)
        malformed_path.write_text("{not valid json at all")

        before_valid = valid_path.read_bytes()
        before_malformed = malformed_path.read_bytes()

        with pytest.raises(DedupRepairAmbiguousError):
            repair_duplicate_and_shifted_points(tmp_path)

        assert valid_path.read_bytes() == before_valid
        assert malformed_path.read_bytes() == before_malformed
        assert not _marker_path(tmp_path).exists()

    def test_record_missing_id_field_raises_untouched(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        valid_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:h4i",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        no_id_path = tmp_path / "ee" / "ff" / "vector_no_id_field.json"
        no_id_path.parent.mkdir(parents=True, exist_ok=True)
        no_id_path.write_text(
            json.dumps({"vector": [0.1, 0.2, 0.3, 0.4], "payload": {}})
        )

        before = valid_path.read_bytes()

        with pytest.raises(DedupRepairAmbiguousError):
            repair_duplicate_and_shifted_points(tmp_path)

        assert valid_path.read_bytes() == before
        assert not _marker_path(tmp_path).exists()

    def test_invalid_utf8_record_raises_untouched(self, tmp_path: Path) -> None:
        """Codex finding 3: open()/json.load() raises UnicodeDecodeError
        for a genuinely invalid-UTF-8 file -- this MUST be classified as
        malformed (fail loud, pre-mutation) rather than escaping the
        malformed-record collector as an unhandled exception."""
        _write_collection_meta(tmp_path)
        valid_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:utf8",
            index=5,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        bad_utf8_path = (
            tmp_path / "11" / "22" / "vector_bad00000000000000000000000utf8.json"
        )
        bad_utf8_path.parent.mkdir(parents=True, exist_ok=True)
        # Genuinely invalid UTF-8 byte sequence (lone continuation byte).
        bad_utf8_path.write_bytes(b'{"id": "\xff\xfe", "vector": [0.1]}')

        before_valid = valid_path.read_bytes()
        before_bad = bad_utf8_path.read_bytes()

        with pytest.raises(DedupRepairAmbiguousError):
            repair_duplicate_and_shifted_points(tmp_path)

        assert valid_path.read_bytes() == before_valid
        assert bad_utf8_path.read_bytes() == before_bad
        assert not _marker_path(tmp_path).exists()


class TestWholeCollectionIdentityGate:
    """Codex H5 (foreign identity formats) + Claude F2 (mixed unique_key
    presence), unified: repair proceeds ONLY IF every scanned record has
    a parseable unique_key AND md5(unique_key) == its stored point_id.
    Any violation passes the WHOLE collection through untouched --
    migration proceeds exactly as it did before this repair existed."""

    def test_foreign_identity_format_with_duplicate_passes_through_untouched(
        self, tmp_path: Path
    ) -> None:
        """Simulates records written by a different pipeline (e.g.
        GitAwareDocumentProcessor) using colon-delimited unique_key and
        UUID point_ids -- structurally incompatible with this repair's
        md5-of-underscore-key identity scheme."""
        _write_collection_meta(tmp_path)
        foreign_id = str(uuid.uuid4())
        p1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:for",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            point_id_override=foreign_id,
            unique_key_override="proj:sha256:for:0",
            shard_suffix="-a",
        )
        p2 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:for",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            point_id_override=foreign_id,
            unique_key_override="proj:sha256:for:0",
            shard_suffix="-b",
        )
        before = {p1: p1.read_bytes(), p2: p2.read_bytes()}

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.gate_passed is False
        assert p1.read_bytes() == before[p1]
        assert p2.read_bytes() == before[p2]
        assert not _marker_path(tmp_path).exists()

        # Pre-#1502 behavior intact: the collection's OWN duplicate is
        # still detected (and still fails loud) by the pre-existing scan
        # this repair never touched.
        with pytest.raises(DuplicateSourceIdError):
            IDIndexManager().scan_vectors_for_id_map_verbose(tmp_path)

    def test_mixed_unique_key_presence_passes_through_untouched(
        self, tmp_path: Path
    ) -> None:
        """One record carries a proper, self-consistent unique_key; a
        SECOND, otherwise-unrelated record has none at all. Claude F2:
        the OLD per-record skip could forge a NEW id collision between a
        renumbered record and a skipped one -- the whole-collection gate
        closes this by refusing the ENTIRE collection instead."""
        _write_collection_meta(tmp_path)
        with_key_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:mix",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        without_key_path = _write_record(
            tmp_path,
            project_id="other",
            file_hash="sha256:noo",
            index=0,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=1,
            line_end=10,
            omit_unique_key=True,
        )
        before = {
            with_key_path: with_key_path.read_bytes(),
            without_key_path: without_key_path.read_bytes(),
        }

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.gate_passed is False
        assert with_key_path.read_bytes() == before[with_key_path]
        assert without_key_path.read_bytes() == before[without_key_path]
        assert not _marker_path(tmp_path).exists()

    def test_md5_self_check_mismatch_passes_through_untouched(
        self, tmp_path: Path
    ) -> None:
        """A record whose unique_key PARSES fine but whose stored point_id
        does not equal md5(unique_key) is not self-consistent -- must
        also trip the gate (not merely "parseable")."""
        _write_collection_meta(tmp_path)
        mismatched_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:mis",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            point_id_override="ffffffffffffffffffffffffffffffff",
        )
        before = mismatched_path.read_bytes()

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.gate_passed is False
        assert mismatched_path.read_bytes() == before


class TestCrashWindowRecovery:
    """Codex C1/C2: a marker file written durably BEFORE the first
    mutation forces the id_index.bin/HNSW rebuild to run on the NEXT
    invocation even if that invocation's own planning finds nothing to
    change -- converging a crash-interrupted prior run to consistency."""

    def test_marker_present_with_already_canonical_records_still_rebuilds(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:crw",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:crw",
            index=1,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
        )
        # Simulate: a PRIOR run wrote the marker before mutating, then
        # crashed before the rebuild ever ran. Derived artifacts are
        # therefore stale/absent (no id_index.bin, no hnsw_index.bin).
        _marker_path(tmp_path).write_text(json.dumps({"pending_since": 0}))

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.duplicates_found == 0
        assert result.records_renumbered == 0  # already canonical
        assert result.id_index_rebuilt is True
        assert result.hnsw_rebuilt is True
        assert not _marker_path(tmp_path).exists()
        _assert_derived_artifacts_consistent(tmp_path)

    def test_marker_present_with_partial_renumber_converges(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        # One record ALREADY at its true canonical position...
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:prt",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        # ...one record still carrying its OLD, shifted label (as if a
        # prior crashed run rewrote the first record but not this one).
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:prt",
            index=9,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
        )
        _marker_path(tmp_path).write_text(json.dumps({"pending_since": 0}))

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.records_renumbered >= 1
        assert result.id_index_rebuilt is True
        assert result.hnsw_rebuilt is True
        assert not _marker_path(tmp_path).exists()

        expected_id_0 = _point_id("proj", "sha256:prt", 0)
        expected_id_1 = _point_id("proj", "sha256:prt", 1)
        assert _current_json_tree_ids(tmp_path) == {expected_id_0, expected_id_1}
        _assert_derived_artifacts_consistent(tmp_path)


class TestEmptyTreeWithStaleMarkerFailsLoud:
    """Codex HIGH finding 2: a stale crash marker combined with a
    post-scan EMPTY JSON tree is anomalous (either a genuinely-always-
    empty collection should never carry this marker, or something
    unexpectedly deleted every record mid-repair). Silently "converging"
    here would call HNSWIndexManager.rebuild_from_vectors with zero
    files on disk, which returns early WITHOUT deleting a pre-existing
    stale hnsw_index.bin -- a false convergence with stale vectors still
    queryable. Must fail loud instead, and must NEVER touch the marker
    (leaving crash evidence intact for manual review)."""

    def test_stale_marker_with_empty_tree_raises_and_preserves_stale_artifacts(
        self, tmp_path: Path
    ) -> None:
        # A pre-existing (now-stale) HNSW index left behind by data that
        # is no longer present -- simulating the exact false-convergence
        # hazard: if repair silently "succeeded" here, this stale file
        # would remain queryable forever.
        _write_collection_meta(tmp_path, {"0": "stale-id-should-never-resolve"})
        stale_hnsw_bytes = b"stale-hnsw-index-bytes-must-survive"
        (tmp_path / "hnsw_index.bin").write_bytes(stale_hnsw_bytes)
        _marker_path(tmp_path).write_text(json.dumps({"pending_since": 0}))

        with pytest.raises(DedupRepairAmbiguousError):
            repair_duplicate_and_shifted_points(tmp_path)

        # Marker NOT deleted on the error path -- crash evidence intact.
        assert _marker_path(tmp_path).exists()
        # The stale hnsw_index.bin is untouched (proves no silent
        # "successful" rebuild-over-nothing occurred).
        assert (tmp_path / "hnsw_index.bin").read_bytes() == stale_hnsw_bytes

    def test_empty_tree_without_marker_is_still_a_clean_no_op(
        self, tmp_path: Path
    ) -> None:
        """A genuinely-always-empty collection (no marker at all) is NOT
        anomalous -- must remain the pre-existing clean no-op."""
        _write_collection_meta(tmp_path)

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.records_scanned == 0
        assert not _marker_path(tmp_path).exists()


class TestAuthoritativeHnswBuildParams:
    """Codex LOW finding 5: vector dimension and HNSW distance metric are
    read ONLY from collection_meta.json (never sniffed from vector data,
    never defaulted to a hardcoded constant) and fail loud, PRE-mutation,
    when undeterminable -- silently rebuilding with the WRONG dimension
    or metric would corrupt the index."""

    def test_missing_vector_dim_fails_loud_before_any_mutation(
        self, tmp_path: Path
    ) -> None:
        # No vector_size, no hnsw_index at all -- dimension undeterminable.
        (tmp_path / "collection_meta.json").write_text(json.dumps({"name": "coll"}))
        record_a = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:dim",
            index=5,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        record_b = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:dim",
            index=6,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
        )
        before = {record_a: record_a.read_bytes(), record_b: record_b.read_bytes()}

        with pytest.raises(DedupRepairAmbiguousError):
            repair_duplicate_and_shifted_points(tmp_path)

        assert record_a.read_bytes() == before[record_a]
        assert record_b.read_bytes() == before[record_b]
        assert not _marker_path(tmp_path).exists()

    def test_missing_hnsw_space_fails_loud_before_any_mutation(
        self, tmp_path: Path
    ) -> None:
        # vector_size present (dimension determinable) but NO hnsw_index
        # at all -- distance metric undeterminable.
        (tmp_path / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": 4})
        )
        record_a = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:spc",
            index=5,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        record_b = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:spc",
            index=6,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
        )
        before = {record_a: record_a.read_bytes(), record_b: record_b.read_bytes()}

        with pytest.raises(DedupRepairAmbiguousError):
            repair_duplicate_and_shifted_points(tmp_path)

        assert record_a.read_bytes() == before[record_a]
        assert record_b.read_bytes() == before[record_b]
        assert not _marker_path(tmp_path).exists()


class TestHiddenBranchFilteringPreservedAcrossRebuild:
    """Codex HIGH finding 1: the rebuild must preserve Bug #306's
    hidden_branches branch-isolation semantics by threading the
    collection's recorded current_branch through to
    HNSWIndexManager.rebuild_from_vectors -- reusing that EXACT existing
    mechanism, never reimplementing filtering. Discriminating both ways:
    a real, production-shaped branch-aware build BEFORE repair proves
    the record was hidden pre-repair too; the SAME assertion after
    repair proves the rebuild preserved it."""

    def test_hidden_record_stays_excluded_before_and_after_repair(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        visible_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:branch",
            index=5,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        hidden_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:branch",
            index=6,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
            extra_payload={"hidden_branches": ["feature-x"]},
        )
        visible_old_id = _point_id("proj", "sha256:branch", 5)
        hidden_old_id = _point_id("proj", "sha256:branch", 6)

        # CONTROL: a real branch-aware build BEFORE repair, using the
        # SAME hidden_branches/current_branch mechanism repair itself
        # reuses -- proves the hidden record is excluded pre-repair too
        # (this is the "discriminating both ways" half of the test).
        HNSWIndexManager(vector_dim=4, space="cosine").rebuild_from_vectors(
            tmp_path, current_branch="feature-x"
        )
        pre_repair_meta = json.loads((tmp_path / "collection_meta.json").read_text())
        pre_repair_ids = set(pre_repair_meta["hnsw_index"]["id_mapping"].values())
        assert visible_old_id in pre_repair_ids
        assert hidden_old_id not in pre_repair_ids

        # The bare rebuild_from_vectors(current_branch=..., visible_files=
        # None) API call above deliberately does NOT persist
        # hnsw_index.current_branch (see test_hnsw_branch_isolation.py's
        # own test_rebuild_from_vectors_current_branch_not_stored_for_
        # unfiltered_rebuild -- an existing, intentional design this test
        # must not contradict). In REAL production that persistence
        # happens via FilesystemVectorStore.rebuild_hnsw_filtered, which
        # always supplies a visible_files set (filtered=True). Simulate
        # that already-persisted real-world state directly, since this
        # test's focus is repair's REUSE of the recorded value, not the
        # separate, already-tested persistence mechanism itself.
        pre_repair_meta["hnsw_index"]["current_branch"] = "feature-x"
        (tmp_path / "collection_meta.json").write_text(json.dumps(pre_repair_meta))

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.hnsw_rebuilt is True
        assert visible_path.exists()
        assert hidden_path.exists()

        post_meta = json.loads((tmp_path / "collection_meta.json").read_text())
        post_ids = set(post_meta["hnsw_index"]["id_mapping"].values())
        visible_new_id = _point_id("proj", "sha256:branch", 0)
        hidden_new_id = _point_id("proj", "sha256:branch", 1)
        assert visible_new_id in post_ids, "visible record must remain queryable"
        assert hidden_new_id not in post_ids, (
            "hidden record must STILL be excluded from the rebuilt HNSW "
            "index after repair -- branch isolation semantics regressed"
        )
        # The vector JSON file itself is untouched by branch isolation --
        # only the HNSW index excludes it (matches production semantics:
        # switching branches back must still see the data).
        hidden_data = json.loads(hidden_path.read_text())
        assert hidden_data["payload"]["hidden_branches"] == ["feature-x"]

    def test_current_branch_persisted_after_repair_and_survives_later_rebuild(
        self, tmp_path: Path
    ) -> None:
        """Codex MEDIUM (round-3 delta): repair's rebuild calls
        HNSWIndexManager.rebuild_from_vectors(current_branch=...,
        visible_files=None) -- HNSWIndexManager._update_metadata
        deliberately persists hnsw_index.current_branch ONLY inside its
        `filtered` (visible_files-driven) branch, so the branch context
        genuinely APPLIED to filtering during repair's rebuild was
        otherwise silently lost from collection_meta.json. A LATER
        rebuild (e.g. a query-time staleness rebuild) that reads ONLY
        the persisted metadata would then see no branch context and
        incorrectly re-include the previously-hidden vector.

        Fixture note: records are deliberately written with SHIFTED old
        labels (index=5/6, matching the SAME pattern as the sibling test
        above) so repair's renumber phase forces the rebuild to fire
        (per the finding's own instruction: "run repair with a mutation
        so rebuild fires"). Repair sorts by line_start
        (visible line_start=1 -> new_index=0; hidden line_start=11 ->
        new_index=1), so post-repair ids are keyed off the CANONICAL
        0/1 indices, never the original 5/6 -- exactly as the sibling
        test's own already-passing assertions already establish.
        """
        _write_collection_meta(tmp_path)
        visible_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:branchpersist",
            index=5,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        hidden_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:branchpersist",
            index=6,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
            extra_payload={"hidden_branches": ["feature-y"]},
        )
        assert visible_path.exists()
        assert hidden_path.exists()

        # Simulate the real-world already-persisted state (see rationale
        # in the sibling test above): current_branch recorded, as a real
        # FilesystemVectorStore.rebuild_hnsw_filtered call would leave it.
        meta = json.loads((tmp_path / "collection_meta.json").read_text())
        meta.setdefault("hnsw_index", {"version": 1, "id_mapping": {}})
        meta["hnsw_index"]["current_branch"] = "feature-y"
        (tmp_path / "collection_meta.json").write_text(json.dumps(meta))

        result = repair_duplicate_and_shifted_points(tmp_path)
        assert result.hnsw_rebuilt is True

        post_meta = json.loads((tmp_path / "collection_meta.json").read_text())
        assert post_meta["hnsw_index"].get("current_branch") == "feature-y", (
            "hnsw_index.current_branch must STILL be recorded in "
            "collection_meta.json after repair's rebuild -- otherwise a "
            "later rebuild reading only the persisted metadata has no "
            "branch context and will silently re-index hidden vectors"
        )

        # SECOND, INDEPENDENT rebuild reading ONLY the persisted
        # metadata's current_branch -- simulates a later query-time
        # staleness rebuild that has no other way to learn the branch.
        # Repair's renumber phase already relabeled both survivors to
        # their canonical 0/1 indices (see fixture note above), so the
        # id_mapping this second rebuild produces is keyed by THOSE new
        # indices, never the original 5/6 written above.
        second_rebuild_branch = post_meta["hnsw_index"].get("current_branch")
        HNSWIndexManager(vector_dim=4, space="cosine").rebuild_from_vectors(
            tmp_path, current_branch=second_rebuild_branch
        )
        after_second_rebuild_meta = json.loads(
            (tmp_path / "collection_meta.json").read_text()
        )
        second_rebuild_ids = set(
            after_second_rebuild_meta["hnsw_index"]["id_mapping"].values()
        )
        visible_new_id = _point_id("proj", "sha256:branchpersist", 0)
        hidden_new_id = _point_id("proj", "sha256:branchpersist", 1)
        assert visible_new_id in second_rebuild_ids
        assert hidden_new_id not in second_rebuild_ids, (
            "a later rebuild driven ONLY by the persisted current_branch "
            "must still exclude the hidden record -- branch context was "
            "lost after repair's own rebuild"
        )


class TestCrashInjectionPhaseBoundaries:
    """Codex test-quality finding 7a: inject a crash at EACH phase
    boundary (quarantine -> JSON-rewrite -> id_index-rebuild ->
    HNSW-rebuild -> marker-delete), confirm the injected exception
    propagates with the marker left in place, then retry with the crash
    removed and confirm full convergence (consistent id_index + HNSW +
    tree, marker gone)."""

    def _make_fixture(self, tmp_path: Path) -> None:
        """One duplicate pair (exercises quarantine) whose sole survivor
        also needs renumbering (exercises the JSON-rewrite phase too)."""
        _write_collection_meta(tmp_path)
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:crash",
            index=7,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:crash",
            index=7,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:crash", 7)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

    def _crash_then_converge(self, tmp_path: Path, monkeypatch, attr_name: str) -> None:
        def _raiser(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"injected crash at {attr_name}")

        with monkeypatch.context() as m:
            m.setattr(repair_mod, attr_name, _raiser)
            with pytest.raises(RuntimeError, match="injected crash"):
                repair_duplicate_and_shifted_points(tmp_path)

        # Crash evidence intact: marker survives the failed attempt.
        assert _marker_path(tmp_path).exists(), (
            f"marker must survive a crash at {attr_name}"
        )

        # Retry with the crash removed -- must converge cleanly.
        result = repair_duplicate_and_shifted_points(tmp_path)

        assert not _marker_path(tmp_path).exists()
        assert result.id_index_rebuilt is True
        assert result.hnsw_rebuilt is True
        _assert_derived_artifacts_consistent(tmp_path)

    def test_crash_during_quarantine_then_retry_converges(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._make_fixture(tmp_path)
        self._crash_then_converge(tmp_path, monkeypatch, "_quarantine_loser")

    def test_crash_during_json_rewrite_then_retry_converges(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._make_fixture(tmp_path)
        self._crash_then_converge(tmp_path, monkeypatch, "_atomic_write_json_record")

    def test_crash_during_id_index_rebuild_then_retry_converges(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._make_fixture(tmp_path)

        def _raiser(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("injected crash at id_index rebuild")

        with monkeypatch.context() as m:
            m.setattr(IDIndexManager, "rebuild_from_vectors", _raiser)
            with pytest.raises(RuntimeError, match="injected crash"):
                repair_duplicate_and_shifted_points(tmp_path)

        assert _marker_path(tmp_path).exists()

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert not _marker_path(tmp_path).exists()
        assert result.id_index_rebuilt is True
        assert result.hnsw_rebuilt is True
        _assert_derived_artifacts_consistent(tmp_path)

    def test_crash_during_hnsw_rebuild_then_retry_converges(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._make_fixture(tmp_path)

        def _raiser(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("injected crash at HNSW rebuild")

        with monkeypatch.context() as m:
            m.setattr(HNSWIndexManager, "rebuild_from_vectors", _raiser)
            with pytest.raises(RuntimeError, match="injected crash"):
                repair_duplicate_and_shifted_points(tmp_path)

        assert _marker_path(tmp_path).exists()

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert not _marker_path(tmp_path).exists()
        assert result.id_index_rebuilt is True
        assert result.hnsw_rebuilt is True
        _assert_derived_artifacts_consistent(tmp_path)

    def test_crash_during_marker_delete_then_retry_converges(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        self._make_fixture(tmp_path)
        self._crash_then_converge(tmp_path, monkeypatch, "_delete_marker_durably")


class TestRepairEmptyCollection:
    def test_empty_collection_is_a_clean_no_op(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)

        result = repair_duplicate_and_shifted_points(tmp_path)

        assert result.records_scanned == 0
        assert result.duplicates_found == 0
        assert result.records_renumbered == 0
        assert not _marker_path(tmp_path).exists()
