"""
Codex review Finding F3: `consolidate_collection_in_place()` genuinely
produces a distinct `status="dedup_deletion_gated"` result (Story #1560
AC19) when the Story #1460 rollout gate is closed AND real duplicate-
point-id groups are present -- but `orchestrator.py`'s
`_consolidate_collections()` never inspected that status at all, so it
was neither counted as consolidated nor as disk-skipped. The overall
`FleetMigrationRepoResult` then collapsed to the GENERIC "incomplete"
status via the final disk/temporal-completeness fallback in
`_run_migration_sequence()`. Back in `FleetMigrationScheduler.
_run_next_candidate()`, `status_counts_as_quarantine_failure("incomplete")`
returns True, so `consecutive_failure_count` was incremented on EVERY
tick for a repo whose ONLY problem is "the operator hasn't flipped
fleet_migration_config.enabled yet" -- eventually quarantining it. This
directly violates Design decision 7 ("this cause must NEVER quarantine,
unconditionally").

This test drives the REAL production entry point
(`run_fleet_migration_for_repo`) with `deletion_authorized=False` and a
real on-disk duplicate-point-id collection, proving the overall result
status is a DISTINCT, non-generic value, and that
`status_counts_as_quarantine_failure()` excludes it.

Real WriteLockManager (via a real RefreshScheduler), real on-disk
duplicate-point-id collection -- no mocking of the orchestrator/repair
mechanism under test.
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
_VECTOR_COMPONENTS = [0.1, 0.2, 0.3, 0.4]
_LINE_START = 1
_LINE_END = 10
_SHARD_PREFIX_LEN = 2
_SHARD_SUFFIX_LEN = 2
_SINGLE_RECORD_CHUNK_INDEX = 5
_DUPLICATE_GROUP_CHUNK_INDEX = 0
_TOTAL_CHUNKS = 1
_HNSW_METADATA_VERSION = 1
_INITIAL_VECTOR_COUNT = 0
_EXPECTED_DUPLICATE_FILE_COUNT = 2


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _shard_dir(collection_dir: Path, point_id: str, suffix: str = "") -> Path:
    prefix = point_id[:_SHARD_PREFIX_LEN]
    mid = point_id[_SHARD_PREFIX_LEN : _SHARD_PREFIX_LEN + _SHARD_SUFFIX_LEN]
    return collection_dir / prefix / (mid + suffix)


def _write_duplicate_pair(collection_dir: Path) -> None:
    """Two legacy sharded records sharing the SAME point_id -- no
    id_index.bin entry, resolved as a "no winner" group."""
    project_id, file_hash, index = (
        "proj",
        "sha256:gatedtest",
        _DUPLICATE_GROUP_CHUNK_INDEX,
    )
    point_id = _point_id(project_id, file_hash, index)
    for shard_suffix, content_suffix in (("-a", "a"), ("-b", "b")):
        # Dict[str, Any]: raw on-disk JSON vector-record payload,
        # mirroring production's own untyped shape (no Pydantic model
        # exists for the legacy sharded record format).
        payload: Dict[str, Any] = {
            "path": "src/foo.py",
            "content": f"chunk content {content_suffix}",
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
        record = {"id": point_id, "vector": _VECTOR_COMPONENTS, "payload": payload}
        shard_dir = _shard_dir(collection_dir, point_id, shard_suffix)
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _write_single_nonduplicated_record(collection_dir: Path) -> None:
    """A single, non-duplicated record -- the gate has nothing to defer."""
    project_id, file_hash, index = "proj", "sha256:nodup", _SINGLE_RECORD_CHUNK_INDEX
    point_id = _point_id(project_id, file_hash, index)
    record = {
        "id": point_id,
        "vector": _VECTOR_COMPONENTS,
        "payload": {
            "path": "src/foo.py",
            "point_id": point_id,
            "unique_key": f"{project_id}_{file_hash}_{index}",
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
        # stop() is idempotent and a safe no-op when the scheduler's own
        # background thread was never started (none of these tests call
        # start()) -- called anyway for defense-in-depth against any
        # future test in this module that does.
        scheduler.stop()


class TestDedupDeletionGatedStatusNeverCollapsesToGeneric:
    def test_gated_duplicate_produces_distinct_status_not_generic_incomplete(
        self, tmp_path: Path, refresh_scheduler: RefreshScheduler
    ) -> None:
        base_clone = tmp_path / "golden-repos" / "click"
        index_path = base_clone / ".code-indexer" / "index"
        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir(parents=True)
        (base_clone / ".code-indexer" / "config.json").write_text("{}")
        _write_collection_meta(collection_dir)
        _write_duplicate_pair(collection_dir)

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=refresh_scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="click",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[collection_dir],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=False,
        )

        assert result.status != "incomplete", (
            "a dedup-deletion-gated collection must never collapse into "
            "the GENERIC 'incomplete' status -- it needs its own "
            "distinct, non-quarantining status"
        )
        assert not status_counts_as_quarantine_failure(result.status), (
            f"status {result.status!r} must be excluded from the "
            f"quarantine breaker -- Design decision 7: this cause must "
            f"NEVER quarantine, unconditionally"
        )
        # Nothing was mutated -- the duplicate pair is still present.
        assert (
            len(list(collection_dir.rglob("vector_*.json")))
            == _EXPECTED_DUPLICATE_FILE_COUNT
        )

    def test_no_duplicate_still_reports_generic_incomplete_when_gated(
        self, tmp_path: Path, refresh_scheduler: RefreshScheduler
    ) -> None:
        """Regression: Story #1460's EXISTING "gated with no duplicate"
        scenario (a plain mixed-layout bake window, nothing to do with
        dedup) must keep reporting the generic 'incomplete' status --
        this fix is scoped ONLY to the genuine duplicate-detected case."""
        base_clone = tmp_path / "golden-repos" / "click"
        index_path = base_clone / ".code-indexer" / "index"
        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir(parents=True)
        (base_clone / ".code-indexer" / "config.json").write_text("{}")
        _write_collection_meta(collection_dir)
        _write_single_nonduplicated_record(collection_dir)

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=refresh_scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="click",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[collection_dir],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=False,
        )

        assert result.status == "incomplete"
