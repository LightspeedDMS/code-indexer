"""Bug #1562: fleet-migration jobs report progress=25 for their entire
multi-hour lifetime, indistinguishable from a hang.

Root cause: `consolidate_collection_in_place()` (storage/shared/
collection_migration.py) -- where ALL the time is spent (the write+verify
loop over legacy vector_*.json records, and the legacy-file deletion loop)
-- has zero progress instrumentation. This module proves, at the lowest
level where the actual long-running work happens, that an optional
`progress_callback` is invoked with genuinely ADVANCING values (not a
single constant, and not merely fluctuating) during a real multi-batch
consolidation, using a real filesystem + real SQLite (ChunkStore) -- no
mocking of the storage layer under test, and no monkeypatching of
production module state (records are sized to exercise the REAL,
production `_MIGRATION_BATCH_SIZE` across more than one batch).
"""

import json
from pathlib import Path
from typing import List, Optional, Tuple

from code_indexer.storage.shared import collection_migration
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout

#: The REAL production batch size this module writes+verifies at a time
#: (collection_migration._MIGRATION_BATCH_SIZE) -- read dynamically rather
#: than hardcoded, so this test tracks the production value if it ever
#: changes, and exercises the actual batching behavior rather than a
#: test-only override of it.
_PRODUCTION_BATCH_SIZE = collection_migration._MIGRATION_BATCH_SIZE

#: How many records beyond one full batch are needed so the write+verify
#: loop must run a SECOND, partial batch -- the minimum needed to prove
#: intra-phase progress ticks more than once.
_BATCH_REMAINDER_COUNT = 5

#: Enough legacy records that the write+verify loop must run at least TWO
#: batches (one full batch of _PRODUCTION_BATCH_SIZE, plus a remainder).
_RECORD_COUNT_SPANNING_TWO_BATCHES = _PRODUCTION_BATCH_SIZE + _BATCH_REMAINDER_COUNT

#: A small collection used only to exercise the deletion-phase naming
#: check -- deliberately far smaller than the write-phase fixture above,
#: since that test only needs the deletion loop to run at all, not to
#: span multiple internal ticks.
_SMALL_RECORD_COUNT = 6

#: Point-id shape used by this module's real hash-sharded legacy layout:
#: an 8-hex-digit id, sharded two levels deep as id[0:2]/id[2:4]/.
_POINT_ID_HEX_WIDTH = 8
_SHARD_LEVEL_1_END = 2
_SHARD_LEVEL_2_END = 4

#: A small fixed-size sample vector -- dimension is irrelevant to this
#: test (it only cares about progress reporting, not embedding content).
_SAMPLE_VECTOR = [0.1, 0.2]
_SAMPLE_VECTOR_DIMENSION = len(_SAMPLE_VECTOR)


def _write_vector_json(
    collection_dir: Path, point_id: str, vector: List[float]
) -> None:
    shard_dir = (
        collection_dir
        / point_id[:_SHARD_LEVEL_1_END]
        / point_id[_SHARD_LEVEL_1_END:_SHARD_LEVEL_2_END]
    )
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {},
        "payload": {"path": "src/a.py"},
        "chunk_text": "x",
    }
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _make_collection_with_records(tmp_path: Path, count: int) -> Path:
    collection_dir = tmp_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": _SAMPLE_VECTOR_DIMENSION})
    )
    for i in range(count):
        point_id = f"{i:0{_POINT_ID_HEX_WIDTH}x}"
        _write_vector_json(collection_dir, point_id, _SAMPLE_VECTOR)
    return collection_dir


class _ProgressRecorder:
    """Test double capturing every progress_callback invocation IN ORDER
    -- NOT a mock of anything under test, a plain recording collaborator."""

    def __init__(self) -> None:
        self.calls: List[Tuple[int, Optional[str], Optional[str]]] = []

    def __call__(
        self, progress: int, phase: Optional[str] = None, detail: Optional[str] = None
    ) -> None:
        self.calls.append((progress, phase, detail))


class TestConsolidateCollectionInPlaceProgressCallback:
    def test_progress_advances_through_multiple_distinct_values_during_write_phase(
        self, tmp_path: Path
    ) -> None:
        """The write+verify loop is where the reported 2h11m of the real
        staging incident was spent. This fixture spans two REAL production
        -sized batches, proving genuine MONOTONIC advancement -- not merely
        more than one distinct value (which a fluctuating 40,30,40
        sequence would also satisfy) and not merely a change caused by
        some OTHER phase's transition."""
        collection_dir = _make_collection_with_records(
            tmp_path, count=_RECORD_COUNT_SPANNING_TWO_BATCHES
        )
        recorder = _ProgressRecorder()

        result = consolidate_collection_in_place(
            collection_dir, progress_callback=recorder
        )

        assert result.status == "consolidated"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB

        # Filter strictly to callbacks tagged as the WRITE phase, IN THE
        # ORDER THEY WERE RECEIVED -- the long-running loop the real
        # staging incident actually stalled in. Looking at ALL calls
        # combined (scan/write/verify/flip/delete) could pass even if
        # every write-batch tick reported the SAME value, as long as some
        # other phase transition changed it -- that would not prove the
        # write loop itself advances.
        write_phase_values = [
            call[0]
            for call in recorder.calls
            if call[1] is not None and "writ" in call[1].lower()
        ]
        assert len(write_phase_values) > 1, (
            f"Bug #1562: progress_callback's WRITE-phase calls reported "
            f"only {len(write_phase_values)} value(s) across "
            f"{_RECORD_COUNT_SPANNING_TWO_BATCHES} record(s) / "
            f"{_PRODUCTION_BATCH_SIZE} batch size. All calls: {recorder.calls}"
        )
        assert write_phase_values == sorted(write_phase_values), (
            f"Bug #1562: WRITE-phase progress values were not monotonically "
            f"non-decreasing -- {write_phase_values} -- a fluctuating "
            f"sequence is just as useless for distinguishing 'advancing' "
            f"from 'hung' as a constant one."
        )
        assert write_phase_values[-1] > write_phase_values[0], (
            f"Bug #1562: WRITE-phase progress never actually increased from "
            f"its first tick to its last -- {write_phase_values}"
        )

    def test_progress_reaches_a_named_deletion_phase_before_completion(
        self, tmp_path: Path
    ) -> None:
        """Real phase checkpoints (scan -> write -> verify -> flip ->
        delete legacy files) must be visible via the `phase` argument, not
        just an opaque number -- proves the deletion phase (fast: ~2181
        files/sec on staging) is distinguishable from the write phase
        (slow: the bulk of the runtime)."""
        collection_dir = _make_collection_with_records(
            tmp_path, count=_SMALL_RECORD_COUNT
        )
        recorder = _ProgressRecorder()

        result = consolidate_collection_in_place(
            collection_dir, progress_callback=recorder
        )

        assert result.status == "consolidated"
        phases_seen = {call[1] for call in recorder.calls if call[1]}
        assert any("delet" in phase.lower() for phase in phases_seen), (
            f"Bug #1562: no distinct deletion-phase name was ever reported "
            f"to progress_callback -- phases seen: {sorted(phases_seen)}"
        )
