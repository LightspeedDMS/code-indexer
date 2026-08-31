"""Bug #1562: fleet-migration jobs report progress=25 for their entire
multi-hour lifetime, indistinguishable from a hang.

`test_collection_migration_progress_1562.py` proves the lowest-level fix
(`consolidate_collection_in_place()` itself genuinely reports progress).
This module proves the NEXT layer up: `run_fleet_migration_for_repo()`
(server/services/fleet_migration/orchestrator.py) -- the function
`FleetMigrationScheduler._run_next_candidate()` actually calls -- must
accept an optional `progress_callback` and genuinely forward it all the
way down to the real per-collection consolidation, using the SAME real
RefreshScheduler/real-filesystem fixture pattern test_orchestrator_1458.py
already established (no mocking of the storage layer under test).
"""

import json
from pathlib import Path
from typing import List, Optional, Tuple

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.fleet_migration.orchestrator import (
    run_fleet_migration_for_repo,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

#: A collection with this many legacy records is enough to prove a real
#: consolidation ran and reported at least one progress tick -- the
#: "genuinely advances through multiple values" claim is already proven
#: at the lower layer (test_collection_migration_progress_1562.py); this
#: layer only needs to prove the callback is forwarded, not re-prove
#: intra-phase granularity.
_RECORD_COUNT = 3

#: A small fixed-size sample vector, identical for every record -- vector
#: CONTENT is irrelevant to this test (it only cares about progress
#: forwarding, not embedding correctness).
_SAMPLE_VECTOR = [0.1, 0.2]
_SAMPLE_VECTOR_DIMENSION = len(_SAMPLE_VECTOR)

#: Point-id shape used by this module's real hash-sharded legacy layout:
#: an 8-hex-digit id, sharded two levels deep as id[0:2]/id[2:4]/.
_POINT_ID_HEX_WIDTH = 8
_SHARD_LEVEL_1_END = 2
_SHARD_LEVEL_2_END = 4

#: Valid progress-percentage bounds every forwarded call must respect.
_PROGRESS_MIN = 0
_PROGRESS_MAX = 100


def _make_scheduler(tmp_path: Path) -> RefreshScheduler:
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


def _setup_base_clone(tmp_path: Path) -> Path:
    base_clone = tmp_path / "base-clone"
    index_path = base_clone / ".code-indexer" / "index"
    index_path.mkdir(parents=True)
    (base_clone / ".code-indexer" / "config.json").write_text("{}")
    return base_clone


class _ProgressRecorder:
    """Test double capturing every progress_callback invocation IN ORDER
    -- NOT a mock of anything under test, a plain recording collaborator."""

    def __init__(self) -> None:
        self.calls: List[Tuple[int, Optional[str], Optional[str]]] = []

    def __call__(
        self, progress: int, phase: Optional[str] = None, detail: Optional[str] = None
    ) -> None:
        self.calls.append((progress, phase, detail))


class TestRunFleetMigrationForRepoProgressCallback:
    def test_forwards_progress_callback_to_real_consolidation(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"

        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir()
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": _SAMPLE_VECTOR_DIMENSION})
        )
        for i in range(_RECORD_COUNT):
            point_id = f"{i:0{_POINT_ID_HEX_WIDTH}x}"
            _write_vector_json(collection_dir, point_id, _SAMPLE_VECTOR)

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))
        recorder = _ProgressRecorder()

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[collection_dir],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
            progress_callback=recorder,
        )

        assert result.status == "completed"
        assert result.collections_consolidated == 1
        assert recorder.calls, (
            "Bug #1562: run_fleet_migration_for_repo() completed a real "
            "consolidation but never invoked the progress_callback it was "
            "given -- the callback was accepted but not actually wired "
            "through to the real per-collection work."
        )
        for progress, phase, _detail in recorder.calls:
            assert _PROGRESS_MIN <= progress <= _PROGRESS_MAX, (
                f"Bug #1562: a forwarded progress value {progress} fell "
                f"outside [{_PROGRESS_MIN}, {_PROGRESS_MAX}] -- rescaling "
                f"into the orchestrator's overall per-repo progress must "
                f"stay bounded."
            )
            assert phase is not None, (
                "Bug #1562: a forwarded progress call carried no phase "
                "name -- an opaque number alone does not distinguish a "
                "genuinely advancing migration from a hang."
            )
        phases_seen = {phase.lower() for _p, phase, _d in recorder.calls if phase}
        assert any("writ" in phase for phase in phases_seen), (
            f"Bug #1562: no write-phase checkpoint was ever forwarded up "
            f"through the orchestrator -- phases seen: {sorted(phases_seen)}"
        )
