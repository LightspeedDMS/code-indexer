"""
Codex review Finding F4: Story #1560 AC20/AC21's query-quiescence-before-
deletion mechanism is fully implemented and unit-tested inside
collection_dedup_repair.py's `repair_duplicate_and_shifted_points()`
(query_tracker/refcount_key params, real drain via
`wait_for_activated_repo_query_drain`) -- but the PRODUCTION call path
(`run_fleet_migration_for_repo()` -> `_run_migration_sequence()` ->
`_consolidate_collections()` -> `consolidate_collection_in_place()`)
never supplies either argument. `quiescing_marked` is therefore always
False in production: destructive duplicate-point-id deletion runs while
a reader may be actively hydrating the very files being deleted.

This test drives the REAL production entry point
(`run_fleet_migration_for_repo`, the exact function
FleetMigrationScheduler._run_next_candidate() calls) with a real
RefreshScheduler carrying a real QueryTracker and a real duplicate-
point-id collection, running the migration itself on a background
thread while the main thread holds an active reference and POLLS the
tracker's own `is_quiescing()` for it to become True -- directly
observing the state transition the orchestrator must trigger, rather
than an indirect timing proxy (a timing-only assertion was tried first
and found NON-discriminating: the surrounding HNSW-rebuild/CoW-
snapshot/alias-swap overhead alone already exceeds any reasonable
drain-delay threshold, so it passed identically whether or not the fix
was wired). Per the coordinator's explicit instruction for this class
of finding, this proves the mechanism ACTUALLY RUNS via the production
call path, not merely that the underlying repair function works when
called directly.

Real WriteLockManager (via a real RefreshScheduler), real QueryTracker,
real on-disk duplicate-point-id collection -- no mocking of the
orchestrator/repair mechanism under test.
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.fleet_migration.orchestrator import (
    FleetMigrationRepoResult,
    run_fleet_migration_for_repo,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

#: Bounded poll budget (Messi Rule #14: every loop must have a provable
#: termination bound) for observing is_quiescing() flip to True.
_POLL_DEADLINE_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.01
_THREAD_JOIN_TIMEOUT_SECONDS = 10.0


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _duplicate_pair_paths(collection_dir: Path, point_id: str) -> List[Path]:
    return [
        collection_dir
        / point_id[:2]
        / (point_id[2:4] + suffix)
        / f"vector_{point_id}.json"
        for suffix in ("-a", "-b")
    ]


def _write_duplicate_pair(collection_dir: Path) -> str:
    """Two legacy sharded records sharing the SAME point_id (the real
    Bug #1502 shape: same label, different content, different shard
    subdirectories) -- no id_index.bin entry, so repair resolves this as
    a "no winner" group (AC2: both copies deleted symmetrically).
    Returns the shared point_id."""
    project_id, file_hash, index = "proj", "sha256:quiescetest", 0
    point_id = _point_id(project_id, file_hash, index)
    for content_suffix, file_path in zip(
        ("a", "b"), _duplicate_pair_paths(collection_dir, point_id)
    ):
        # Dict[str, Any]: this is a raw on-disk JSON vector-record
        # payload mirroring production's own untyped shape (no Pydantic
        # model exists for the legacy sharded record format) -- matches
        # test_collection_dedup_repair_1502.py's own identical usage.
        payload: Dict[str, Any] = {
            "path": "src/foo.py",
            "content": f"chunk content {content_suffix}",
            "language": "python",
            "project_id": project_id,
            "file_hash": file_hash,
            "chunk_index": index,
            "total_chunks": 1,
            "line_start": 1,
            "line_end": 10,
            "point_id": point_id,
            "unique_key": f"{project_id}_{file_hash}_{index}",
        }
        record = {"id": point_id, "vector": [0.1, 0.2, 0.3, 0.4], "payload": payload}
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps(record))
    return point_id


def _write_collection_meta(collection_dir: Path) -> None:
    meta = {
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
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _make_refresh_scheduler(tmp_path: Path) -> RefreshScheduler:
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    versioned_base = tmp_path / "versioned"
    versioned_base.mkdir(parents=True, exist_ok=True)
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(versioned_base))
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(),
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        snapshot_manager=snapshot_manager,
        job_tracker=None,
    )


@dataclass
class _Fixture:
    refresh_scheduler: RefreshScheduler
    base_clone: Path
    index_path: Path
    collection_dir: Path
    sister_alias_manager: AliasManager
    sister_root: Path
    duplicate_paths: List[Path]


def _build_fixture(tmp_path: Path) -> _Fixture:
    refresh_scheduler = _make_refresh_scheduler(tmp_path)
    base_clone = tmp_path / "golden-repos" / "click"
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (base_clone / ".code-indexer" / "config.json").write_text("{}")
    _write_collection_meta(collection_dir)
    point_id = _write_duplicate_pair(collection_dir)
    duplicate_paths = _duplicate_pair_paths(collection_dir, point_id)
    assert all(p.exists() for p in duplicate_paths)

    sister_root = tmp_path / "sister"
    sister_alias_manager = AliasManager(str(sister_root / "aliases"))
    return _Fixture(
        refresh_scheduler=refresh_scheduler,
        base_clone=base_clone,
        index_path=index_path,
        collection_dir=collection_dir,
        sister_alias_manager=sister_alias_manager,
        sister_root=sister_root,
        duplicate_paths=duplicate_paths,
    )


@dataclass
class _MigrationRunResult:
    result: Optional[FleetMigrationRepoResult] = None
    error: Optional[BaseException] = field(default=None)


def _run_migration_in_background(
    fx: _Fixture, out: _MigrationRunResult
) -> threading.Thread:
    def _run() -> None:
        try:
            out.result = run_fleet_migration_for_repo(
                refresh_scheduler=fx.refresh_scheduler,
                sister_alias_manager=fx.sister_alias_manager,
                repo_alias="click",
                base_clone_path=fx.base_clone,
                index_path=fx.index_path,
                semantic_collection_dirs=[fx.collection_dir],
                temporal_namespaces=[],
                sister_root=fx.sister_root,
                deletion_authorized=True,
            )
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the test thread
            out.error = exc

    thread = threading.Thread(target=_run)
    thread.start()
    return thread


def _poll_until_quiescing_or_deadline(tracker: QueryTracker, refcount_key: str) -> bool:
    """Bounded poll (Messi Rule #14): returns True the moment
    `is_quiescing(refcount_key)` is observed True, or False once
    `_POLL_DEADLINE_SECONDS` has elapsed without ever observing it."""
    deadline = time.monotonic() + _POLL_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if tracker.is_quiescing(refcount_key):
            return True
        time.sleep(_POLL_INTERVAL_SECONDS)
    return False


class TestProductionOrchestratorQuiescesRealQueries:
    def test_migration_marks_quiescing_on_the_real_shared_tracker(
        self, tmp_path: Path
    ) -> None:
        fx = _build_fixture(tmp_path)
        refcount_key = str(fx.index_path)
        tracker = fx.refresh_scheduler.query_tracker
        tracker.increment_ref(refcount_key)

        out = _MigrationRunResult()
        thread = _run_migration_in_background(fx, out)
        try:
            observed_quiescing = _poll_until_quiescing_or_deadline(
                tracker, refcount_key
            )
        finally:
            # Always release the held reference before joining -- a
            # failed poll must never leave the background migration
            # thread permanently blocked waiting for a drain that will
            # never come.
            tracker.decrement_ref(refcount_key)
            thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)

        assert not thread.is_alive(), (
            f"background migration thread did not finish within "
            f"{_THREAD_JOIN_TIMEOUT_SECONDS}s of join()"
        )
        assert observed_quiescing, (
            "run_fleet_migration_for_repo() never marked the real, "
            "shared QueryTracker's refcount_key as quiescing within "
            f"{_POLL_DEADLINE_SECONDS}s -- the orchestrator is not "
            "threading query_tracker/refcount_key through to the "
            "real repair mechanism"
        )
        assert out.error is None, f"migration raised: {out.error}"
        assert out.result is not None
        assert out.result.status == "completed"
        assert all(not p.exists() for p in fx.duplicate_paths)
        assert tracker.is_quiescing(refcount_key) is False
