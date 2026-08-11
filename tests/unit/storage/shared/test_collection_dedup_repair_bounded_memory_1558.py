"""Bug #1558: fleet migration of a LARGE legacy collection must not
materialize every record's full content (including its embedding vector)
simultaneously in memory during Step 0 (metadata-only dedup + renumber
repair, collection_dedup_repair.py).

Measured on real staging hardware: consolidating a 343,604-record legacy
collection drove ONE uvicorn worker to 6.6 GB RSS with the node's 7.5 GB
swapping, the worker was recycled three times, the job stalled forever, and
the collection ended byte-for-byte unchanged (no data loss, but no
progress -- a permanent resource-exhaustion loop).

Root cause, found by profiling (tracemalloc) rather than guessing, per the
candidates listed in the issue:
  - IDIndexManager.scan_vectors_for_id_map_verbose (candidate #1): peak
    5.5 MB @ N=8000 records -- bounded, NOT the culprit (it only retains
    Path objects).
  - collection_migration.py's batched write+verify loop
    (_MIGRATION_BATCH_SIZE=500): already bounded, confirmed by the issue
    itself and unmodified here.
  - collection_dedup_repair._scan_raw_records's ``loaded_by_path`` dict
    (candidate #3 -- "materializing vectors rather than paths/digests"):
    peak 288.8 MB @ N=8000, 577.0 MB @ N=16000 -- almost exactly linear in
    N, because it retains every scanned record's FULL parsed JSON
    (including the embedding vector and payload content) for the entire
    scan -> plan -> apply lifecycle of Step 0, which runs on every FRESH
    (non-resume) migration BEFORE the already-bounded pipeline even
    starts. Extrapolated to N=343,604 this lands in the multi-GB range
    that matches the real staging incident.

Critically, this retention happens during PLANNING -- _scan_raw_records,
the whole-collection identity gate, and _plan_renumber all run BEFORE
Step 0 even decides whether anything needs to change. So a collection
whose labels are ALREADY canonical (the "identity fast path", zero
mutation, zero HNSW rebuild) still pays the full memory cost -- this is
what TestBoundedMemoryDuringConsolidation below exercises at N=8000,
deliberately WITHOUT forcing a rebuild, so the test itself stays fast
while still proving the fix.

A SEPARATE, small-N test (TestApplyPhasePreservesContent) forces every
record through Step 0's renumber APPLY phase (shuffled labels) to prove
the fix's per-record fresh-disk-read at write time does not lose or
corrupt any content -- kept small because that path also drives a real
HNSW index rebuild.
"""

import hashlib
import json
import struct
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

_VECTOR_DIM = 1024
_PROJECT_ID = "proj"
_FILE_HASH = "sha256:" + "b" * 64

_BYTES_PER_MEGABYTE = 1_000_000

# Arbitrary small linear-congruential-style constants used only to make
# each record's vector deterministic and distinguishable from its
# neighbors (not a statistical/cryptographic requirement).
_VECTOR_VALUE_MULTIPLIER = 31
_VECTOR_VALUE_MODULUS = 97
_VECTOR_VALUE_ROUND_DIGITS = 6
_VECTOR_COMPARISON_ROUND_DIGITS = 4

# Matches FixedSizeChunker-shaped output: each record spans 10 lines,
# consecutive records abut exactly (see _GAP_CONTINUITY_SLACK in
# collection_dedup_repair.py).
_LINE_SPAN_PER_RECORD = 10

# Large enough that the pre-fix O(N) full-record retention clearly exceeds
# the memory ceiling below (288.8 MB measured @ N=8000 for the culprit
# function alone). Canonical (non-shuffled) ordering so Step 0 takes the
# identity fast path -- zero mutation, zero HNSW rebuild -- keeping this
# test fast while the scan/plan phase (the actual bug) still runs in full.
_LARGE_RECORD_COUNT = 8000

# Pre-fix (unbounded) peak for a full consolidate_collection_in_place() run
# at this N lands at or above the _scan_raw_records-alone measurement
# (~289 MB), since consolidate_collection_in_place calls it first, at the
# very start of the traced window. A bounded implementation retains only
# small per-record identity views during planning, so it must stay well
# under this ceiling regardless of N.
_PEAK_MEMORY_CEILING_MB = 150.0

# Small enough that a REAL HNSW index rebuild (forced by shuffling every
# record's canonical index) completes quickly, while still exhaustively
# covering every record for the apply-phase content-preservation proof.
_SMALL_RECORD_COUNT = 40


def _build_synthetic_vector(i: int, vector_dim: int) -> List[float]:
    """Deterministic, distinguishable vector for record ``i``."""
    return [float(i)] + [
        round(
            (i * _VECTOR_VALUE_MULTIPLIER + j)
            % _VECTOR_VALUE_MODULUS
            / _VECTOR_VALUE_MODULUS,
            _VECTOR_VALUE_ROUND_DIGITS,
        )
        for j in range(vector_dim - 1)
    ]


def _write_synthetic_record(
    collection_dir: Path, i: int, old_index: int, vector_dim: int
) -> Tuple[str, Path, Dict[str, Any]]:
    """Write one legacy vector_*.json record for line-position ``i``,
    with its unique_key embedding ``old_index`` (equal to ``i`` for a
    canonical fixture, or shuffled for one that forces renumbering).
    Returns (point_id, file_path, original_field_values).
    """
    unique_key = f"{_PROJECT_ID}_{_FILE_HASH}_{old_index}"
    point_id = hashlib.md5(unique_key.encode()).hexdigest()
    marker = f"marker-{i}"
    vector = _build_synthetic_vector(i, vector_dim)
    line_start = i * _LINE_SPAN_PER_RECORD + 1
    line_end = i * _LINE_SPAN_PER_RECORD + _LINE_SPAN_PER_RECORD

    payload = {
        "path": "src/foo.py",
        "content": marker,
        "language": "python",
        "project_id": _PROJECT_ID,
        "file_hash": _FILE_HASH,
        "chunk_index": old_index,
        "total_chunks": 1,
        "line_start": line_start,
        "line_end": line_end,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}

    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))

    original = {
        "vector": vector,
        "path": payload["path"],
        "content": marker,
        "language": payload["language"],
        "line_start": line_start,
        "line_end": line_end,
        "point_id_before_renumber": point_id,
    }
    return point_id, file_path, original


def _write_collection_meta(collection_dir: Path, vector_dim: int) -> None:
    meta = {
        "name": "coll",
        "vector_size": vector_dim,
        "hnsw_index": {
            "version": 1,
            "vector_dim": vector_dim,
            "space": "cosine",
            "vector_count": 0,
            "id_mapping": {},
        },
    }
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _write_id_index_bin(collection_dir: Path, entries: List[Tuple[str, Path]]) -> None:
    index_file = collection_dir / "id_index.bin"
    with open(index_file, "wb") as f:
        f.write(struct.pack("<I", len(entries)))
        for point_id, relpath in entries:
            id_bytes = point_id.encode("utf-8")
            path_bytes = str(relpath).encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)


def _build_collection(
    collection_dir: Path, n: int, vector_dim: int, *, shuffled: bool
) -> Dict[str, Dict[str, Any]]:
    """Write ``n`` legacy vector_*.json records into ``collection_dir``,
    all belonging to ONE (project_id, file_hash) group, positioned in
    strictly increasing line order (satisfying the gap-continuity
    tolerance).

    When ``shuffled`` is False, each record's embedded unique_key index
    equals its line-order position ``i`` -- _plan_renumber then computes
    identical old/new indices for every record, so Step 0 takes the
    identity fast path (no mutation, no HNSW rebuild).

    When ``shuffled`` is True, the embedded index is the REVERSE of ``i``
    -- forcing _plan_renumber to reassign every record's canonical index,
    so Step 0's APPLY phase rewrites every file.

    Returns a dict keyed by marker -> original field values, for later
    correctness verification.
    """
    collection_dir.mkdir(parents=True, exist_ok=True)
    originals: Dict[str, Dict[str, Any]] = {}
    id_index_entries: List[Tuple[str, Path]] = []

    for i in range(n):
        old_index = (n - 1 - i) if shuffled else i
        point_id, file_path, original = _write_synthetic_record(
            collection_dir, i, old_index, vector_dim
        )
        id_index_entries.append((point_id, file_path.relative_to(collection_dir)))
        originals[original["content"]] = original

    _write_collection_meta(collection_dir, vector_dim)
    _write_id_index_bin(collection_dir, id_index_entries)
    return originals


def _assert_content_preserved(
    collection_dir: Path, originals: Dict[str, Dict[str, Any]], n: int
) -> Dict[str, str]:
    """Verify every original record's content survived Step 0's renumber
    APPLY phase byte-for-byte, keyed by its marker (stable across the
    id/unique_key/chunk_index churn renumbering causes). Returns the
    observed marker -> (post-consolidation) point_id mapping."""
    stored_point_id_by_marker: Dict[str, str] = {}
    with ChunkStore(collection_dir / "chunks.db", immutable=True) as store:
        stored_ids = store.all_point_ids()
        assert len(stored_ids) == n
        found_markers = set()
        for point_id in stored_ids:
            stored = store.read(point_id)
            assert stored is not None
            marker = stored["payload"]["content"]
            assert marker not in found_markers, f"duplicate marker {marker!r}"
            found_markers.add(marker)
            stored_point_id_by_marker[marker] = point_id
            expected = originals[marker]
            assert stored["payload"]["path"] == expected["path"]
            assert stored["payload"]["language"] == expected["language"]
            assert stored["payload"]["line_start"] == expected["line_start"]
            assert stored["payload"]["line_end"] == expected["line_end"]
            stored_vector = [
                round(float(v), _VECTOR_COMPARISON_ROUND_DIGITS)
                for v in stored["vector"]
            ]
            expected_vector = [
                round(float(v), _VECTOR_COMPARISON_ROUND_DIGITS)
                for v in expected["vector"]
            ]
            assert stored_vector == expected_vector, (
                f"vector mismatch for marker {marker!r}"
            )
        assert found_markers == set(originals.keys())
    return stored_point_id_by_marker


@pytest.mark.slow
class TestBoundedMemoryDuringConsolidation:
    def test_peak_memory_bounded_independent_of_collection_size(
        self, tmp_path: Path
    ) -> None:
        collection_dir = tmp_path / "collection"
        originals = _build_collection(
            collection_dir, _LARGE_RECORD_COUNT, _VECTOR_DIM, shuffled=False
        )

        tracemalloc.start()
        try:
            result = consolidate_collection_in_place(collection_dir)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        peak_mb = peak / _BYTES_PER_MEGABYTE
        assert peak_mb < _PEAK_MEMORY_CEILING_MB, (
            f"consolidate_collection_in_place peak traced memory "
            f"{peak_mb:.1f} MB exceeds the {_PEAK_MEMORY_CEILING_MB} MB "
            f"bound for N={_LARGE_RECORD_COUNT} records -- Bug #1558: "
            f"memory must be bounded independent of collection size, not "
            f"scale O(N) with full-record (vector-included) retention "
            f"during Step 0's planning phase"
        )

        assert result.status == "consolidated"
        assert not (collection_dir / "hnsw_index.bin").exists(), (
            "fixture uses canonical ordering deliberately -- Step 0 must "
            "take the identity fast path (no HNSW rebuild) so this test "
            "proves the memory bug fires during PLANNING (scan + identity "
            "gate + renumber plan) even when nothing ends up being "
            "renumbered, while staying fast at N=8000"
        )
        assert verify_collection_fully_migrated(collection_dir) is True
        _assert_content_preserved(collection_dir, originals, _LARGE_RECORD_COUNT)


class TestApplyPhasePreservesContent:
    def test_every_record_survives_forced_renumber_apply_phase(
        self, tmp_path: Path
    ) -> None:
        collection_dir = tmp_path / "collection"
        originals = _build_collection(
            collection_dir, _SMALL_RECORD_COUNT, _VECTOR_DIM, shuffled=True
        )

        result = consolidate_collection_in_place(collection_dir)

        assert result.status == "consolidated"
        assert (collection_dir / "hnsw_index.bin").exists(), (
            "fixture uses shuffled ordering deliberately -- Step 0 must "
            "run the full renumber APPLY phase (which rebuilds the HNSW "
            "index), not the identity fast path, so this test actually "
            "exercises the fix's per-record disk re-read"
        )
        assert verify_collection_fully_migrated(collection_dir) is True
        stored_point_id_by_marker = _assert_content_preserved(
            collection_dir, originals, _SMALL_RECORD_COUNT
        )

        for marker, original in originals.items():
            assert (
                stored_point_id_by_marker[marker]
                != original["point_id_before_renumber"]
            ), (
                f"marker {marker!r} kept its pre-renumber point_id -- "
                f"the renumber apply phase must have reassigned it"
            )
