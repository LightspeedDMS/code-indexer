"""Bug #1502 wiring tests: consolidate_collection_in_place() must run the
metadata-only dedup + renumber repair (collection_dedup_repair.py) as
STEP 0 of the fresh migration path, BEFORE the existing
scan/build/verify/flip/cleanup pipeline -- so a collection carrying
duplicate point_ids (or shifted-but-unique chunk labels) converges to a
clean consolidation instead of raising DuplicateSourceIdError and being
quarantined by the fleet-migration scheduler.
"""

import hashlib
import json
from pathlib import Path
from typing import Optional

import pytest

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    ConsolidationVerificationError,
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
    shard_suffix: str = "",
) -> Path:
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = point_id_override or _point_id(project_id, file_hash, index)
    payload = {
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
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path) -> None:
    # Real production-shaped fixture: hnsw_index.vector_dim/.space are
    # ALWAYS present -- Bug #1502's finding-5 remediation requires these
    # as the sole authoritative source for the HNSW rebuild parameters,
    # matching a collection that has genuinely been through at least one
    # real HNSW build (the only kind this repair ever operates on).
    (collection_dir / "collection_meta.json").write_text(
        json.dumps(
            {
                "name": "coll",
                "vector_size": 4,
                "hnsw_index": {
                    "version": 1,
                    "vector_dim": 4,
                    "space": "cosine",
                    "vector_count": 0,
                    "id_mapping": {},
                },
            }
        )
    )


class TestConsolidateCollectionInPlaceRunsBug1502RepairFirst:
    def test_duplicate_point_id_is_repaired_then_consolidated_successfully(
        self, tmp_path: Path
    ) -> None:
        """The confirmed issue #1502 shape: two files, same point_id,
        different content, id_index.bin references the winner. Without
        the repair wired in, this raises DuplicateSourceIdError."""
        _write_collection_meta(tmp_path)
        # Natural collision: SAME (project_id, file_hash, index) computes
        # the SAME point_id via the real md5(unique_key) formula -- the
        # confirmed real bug shape (two runs, same label, different
        # content), and self-consistent with the whole-collection
        # identity gate.
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:abc",
            index=62,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=5631,
            line_end=5678,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:abc",
            index=62,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=5670,
            line_end=5719,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:abc", 62)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB
        with ChunkStore(tmp_path / "chunks.db") as store:
            assert store.count() == 1
            surviving_id = next(iter(store.all_point_ids()))
            record = store.read(surviving_id)
        # Winner's content (vector [0.1,...]) survives under a canonical id.
        assert list(record["vector"]) == [0.1, 0.2, 0.3, 0.4]

    def test_second_call_on_repaired_collection_is_clean_idempotent_resume(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        # Natural collision (see rationale in the test above).
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:xyz",
            index=3,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:xyz",
            index=3,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        shared_point_id = _point_id("proj", "sha256:xyz", 3)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        first = consolidate_collection_in_place(tmp_path)
        assert first.status == "consolidated"

        second = consolidate_collection_in_place(tmp_path)
        assert second.status == "already_consolidated"
        with ChunkStore(tmp_path / "chunks.db") as store:
            assert store.count() == 1

    def test_shifted_labels_no_duplicate_still_consolidates_with_canonical_ids(
        self, tmp_path: Path
    ) -> None:
        """Confirmed real-data shape: labels shifted from line order but
        no duplicate id collision -- must consolidate cleanly with
        canonical (line-order-derived) ids, not the stale shifted ones."""
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:shift",
            index=8,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:shift",
            index=9,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=11,
            line_end=20,
        )

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        expected_id_0 = _point_id("proj", "sha256:shift", 0)
        expected_id_1 = _point_id("proj", "sha256:shift", 1)
        with ChunkStore(tmp_path / "chunks.db") as store:
            all_ids = set(store.all_point_ids())
        assert all_ids == {expected_id_0, expected_id_1}


class TestConsolidateCollectionInPlaceWrapsRepairAmbiguity:
    """The repair step's DedupRepairAmbiguousError must be re-raised as
    ConsolidationVerificationError -- consolidate_collection_in_place's
    own pre-existing, general-purpose "refuse to flip, collection stays
    SHARDED_JSON" exception type -- preserving the contract other
    pre-existing tests (e.g. test_collection_migration_1458.py's
    malformed-record test) depend on."""

    def test_repair_ambiguous_error_wrapped_as_consolidation_verification_error(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gapwrap",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )
        # A real gap -- repair_duplicate_and_shifted_points raises
        # DedupRepairAmbiguousError for this.
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:gapwrap",
            index=1,
            vector=[0.5, 0.6, 0.7, 0.8],
            line_start=500,
            line_end=510,
        )

        with pytest.raises(ConsolidationVerificationError):
            consolidate_collection_in_place(tmp_path)

        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON
        assert not (tmp_path / "chunks.db").exists()


class TestStaleMarkerCleanupOnResumePath:
    """Codex cleanup finding 6: a stale .dedup-repair-pending marker
    surviving from an OLDER, pre-remediation partial run is unmanaged on
    the RESUME (CHUNKS_DB) path, since repair_duplicate_and_shifted_points
    never runs there -- consolidate_collection_in_place must explicitly
    clear it once the resumed state has been verified consolidated, so it
    can never confuse a future pass."""

    def test_stale_marker_from_older_partial_run_cleared_on_resume(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:markerclean",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
        )

        first = consolidate_collection_in_place(tmp_path)
        assert first.status == "consolidated"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB

        # Simulate a marker left behind by an OLDER, pre-remediation
        # partial run -- repair() never runs again on this resume path,
        # so nothing else would ever clear it.
        marker_path = tmp_path / ".dedup-repair-pending"
        marker_path.write_text(json.dumps({"pending_since": 0}))

        second = consolidate_collection_in_place(tmp_path)

        assert second.status == "already_consolidated"
        assert not marker_path.exists(), (
            "a stale marker must be cleared once the resume path has "
            "verified the consolidated state, so it can never linger "
            "unmanaged and confuse a future pass"
        )
