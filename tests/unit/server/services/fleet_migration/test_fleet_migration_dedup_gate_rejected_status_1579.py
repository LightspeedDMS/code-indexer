"""Bug #1579 Part 3c: the whole-collection identity gate (repair_duplicate_
and_shifted_points) can reject a collection that GENUINELY has duplicate
point_id group(s) present -- a foreign/missing/self-inconsistent unique_key
found on some OTHER, unrelated record elsewhere in the same collection.
Before this fix, orchestrator.py's `_consolidate_collections()` never
inspected `consolidate_collection_in_place`'s new "dedup_gate_rejected"
status at all (that status did not exist), so
`consolidate_collection_in_place` itself crashed with
`DuplicateSourceIdError` -- an unhandled exception propagating straight out
of `run_fleet_migration_for_repo`.

Modeled directly on test_fleet_migration_dedup_gated_status_1560.py (its own
template for wiring a NEW orchestrator status through to
`FleetMigrationRepoResult`), with ONE deliberate contrast: unlike
"dedup_deletion_gated" (Story #1460's rollout flag -- resolves on its own,
NEVER quarantines), "dedup_gate_rejected" needs an actual data/schema fix,
so it is left OUT of `_QUARANTINE_EXEMPT_TRANSIENT_STATUSES` and therefore
counts toward quarantine via the fail-conservative default
(`status_counts_as_quarantine_failure`'s own documented safe-default
direction) -- no change to quarantine.py's exempt set is needed or made.

This test drives the REAL production entry point
(`run_fleet_migration_for_repo`) with `deletion_authorized=True` (bypassing
Story #1460's unrelated rollout gate entirely) and a real on-disk
duplicate-point-id-plus-gate-breaking-record collection -- no mocking of the
orchestrator/repair mechanism under test.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.fleet_migration.orchestrator import (
    run_fleet_migration_for_repo,
)
from code_indexer.server.services.fleet_migration.quarantine import (
    status_counts_as_quarantine_failure,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

# Fixture constants -- named to satisfy the "no magic numbers" rule.
_VECTOR_DIM = 4
_VECTOR_COMPONENTS_A = [0.1, 0.2, 0.3, 0.4]
_VECTOR_COMPONENTS_B = [0.9, 0.9, 0.9, 0.9]
_VECTOR_COMPONENTS_FOREIGN = [0.5, 0.5, 0.5, 0.5]
_LINE_START = 1
_LINE_END = 10
_SHARD_PREFIX_LEN = 2
_SHARD_SUFFIX_LEN = 2
_DUPLICATE_GROUP_CHUNK_INDEX = 0
_FOREIGN_RECORD_CHUNK_INDEX = 5
_TOTAL_CHUNKS = 1
_HNSW_METADATA_VERSION = 1
_INITIAL_VECTOR_COUNT = 0
_EXPECTED_RAW_RECORD_COUNT = 3


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _shard_dir(collection_dir: Path, point_id: str, suffix: str = "") -> Path:
    prefix = point_id[:_SHARD_PREFIX_LEN]
    mid = point_id[_SHARD_PREFIX_LEN : _SHARD_PREFIX_LEN + _SHARD_SUFFIX_LEN]
    return collection_dir / prefix / (mid + suffix)


def _write_duplicate_pair(collection_dir: Path) -> None:
    """Two legacy sharded records sharing the SAME point_id -- a genuine,
    natural collision (self-consistent unique_key on BOTH), no id_index.bin
    entry (irrelevant here: the whole-collection gate rejects BEFORE
    winner-resolution is ever reached)."""
    project_id, file_hash, index = (
        "proj",
        "sha256:gaterejected",
        _DUPLICATE_GROUP_CHUNK_INDEX,
    )
    point_id = _point_id(project_id, file_hash, index)
    for shard_suffix, vector in (
        ("-a", _VECTOR_COMPONENTS_A),
        ("-b", _VECTOR_COMPONENTS_B),
    ):
        payload: Dict[str, Any] = {
            "path": "src/foo.py",
            "content": "chunk content",
            "language": "python",
            "project_id": project_id,
            "file_hash": file_hash,
            "chunk_index": index,
            "total_chunks": _TOTAL_CHUNKS,
            "line_start": _LINE_START,
            "line_end": _LINE_END,
            "point_id": point_id,
            "unique_key": f"{project_id}_{file_hash}_{index}",
        }
        record = {"id": point_id, "vector": vector, "payload": payload}
        shard_dir = _shard_dir(collection_dir, point_id, shard_suffix)
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _write_gate_breaking_record(collection_dir: Path) -> None:
    """Unrelated single record with NO unique_key -- breaks the
    WHOLE-collection identity gate (scope is the entire collection, not
    just this record's own group)."""
    project_id, file_hash, index = (
        "proj",
        "sha256:foreign",
        _FOREIGN_RECORD_CHUNK_INDEX,
    )
    point_id = _point_id(project_id, file_hash, index)
    record = {
        "id": point_id,
        "vector": _VECTOR_COMPONENTS_FOREIGN,
        "payload": {
            "path": "src/bar.py",
            "point_id": point_id,
            # deliberately NO "unique_key" key.
            "chunk_index": index,
            "total_chunks": _TOTAL_CHUNKS,
            "line_start": _LINE_START,
            "line_end": _LINE_END,
        },
    }
    shard_dir = _shard_dir(collection_dir, point_id)
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _write_collection_meta(collection_dir: Path) -> None:
    meta = {
        "name": "coll",
        "vector_size": _VECTOR_DIM,
        "hnsw_index": {
            "version": _HNSW_METADATA_VERSION,
            "vector_dim": _VECTOR_DIM,
            "space": "cosine",
            "vector_count": _INITIAL_VECTOR_COUNT,
            "id_mapping": {},
        },
    }
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


@pytest.fixture
def refresh_scheduler(tmp_path: Path):
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    versioned_base = tmp_path / "versioned"
    versioned_base.mkdir(parents=True, exist_ok=True)
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(versioned_base))
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(),
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        snapshot_manager=snapshot_manager,
        job_tracker=None,
    )
    try:
        yield scheduler
    finally:
        scheduler.stop()


class TestDedupGateRejectedStatusCountsTowardQuarantine:
    def test_gate_rejected_with_duplicates_produces_distinct_status(
        self, tmp_path: Path, refresh_scheduler: RefreshScheduler
    ) -> None:
        base_clone = tmp_path / "golden-repos" / "click"
        index_path = base_clone / ".code-indexer" / "index"
        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir(parents=True)
        (base_clone / ".code-indexer" / "config.json").write_text("{}")
        _write_collection_meta(collection_dir)
        _write_duplicate_pair(collection_dir)
        _write_gate_breaking_record(collection_dir)

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        files_before = sorted(collection_dir.rglob("vector_*.json"))
        assert len(files_before) == _EXPECTED_RAW_RECORD_COUNT, (
            "sanity: fixture has 3 raw records"
        )

        result = run_fleet_migration_for_repo(
            refresh_scheduler=refresh_scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="click",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[collection_dir],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert result.status == "dedup_gate_rejected"
        # Deliberate CONTRAST with dedup_deletion_gated (Story #1460's
        # rollout gate, which resolves on its own and never quarantines):
        # dedup_gate_rejected needs an actual data/schema fix, so it counts
        # toward quarantine via the fail-conservative default -- no entry
        # was added to quarantine.py's _QUARANTINE_EXEMPT_TRANSIENT_STATUSES.
        assert status_counts_as_quarantine_failure(result.status) is True, (
            "dedup_gate_rejected must count toward quarantine (fail-"
            "conservative default) -- unlike dedup_deletion_gated, it does "
            "not resolve on its own"
        )
        assert sorted(collection_dir.rglob("vector_*.json")) == files_before, (
            "nothing must be mutated when consolidation is rejected"
        )
