"""Bug #1562: fleet-migration jobs report progress=25 for their entire
multi-hour lifetime, indistinguishable from a hang.

ROOT CAUSE: `BackgroundJobManager.submit_job()`
(server/repositories/background_jobs.py) injects a `progress_callback`
into a worker ONLY when the worker's signature DECLARES a parameter
named `progress_callback` (`inspect.signature(func)`,
`"progress_callback" in func_signature.parameters`) -- see the docstring
at that call site: "Function manages its own progress via
ProgressPhaseAllocator. Bug #483 Fix: Do NOT emit hardcoded 25% here".
`FleetMigrationScheduler._run_next_candidate` (the function actually
submitted as the job) declared no such parameter, so BGM instead emitted
ONE hardcoded `progress_callback(25)` at start and NEVER called it
again -- the exact "pinned at progress=25" defect this bug describes.

This module proves the fix at the scheduler layer: `_run_next_candidate`
now declares `progress_callback` AND genuinely forwards it into a real,
filesystem-backed migration (reusing the established
`_FakeGoldenRepoManager`/`_make_refresh_scheduler`/
`_build_unconsolidated_repo` fixture pattern from test_scheduler_1458.py
-- no mocking of the storage layer under test).
"""

import inspect
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.fleet_migration.scheduler import (
    FleetMigrationScheduler,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout

#: A small fixed-size sample vector -- vector CONTENT is irrelevant to
#: this test (it only cares about progress forwarding, not embedding
#: correctness).
_SAMPLE_VECTOR = [0.1, 0.2]
_SAMPLE_VECTOR_DIMENSION = len(_SAMPLE_VECTOR)

#: Point-id shape used by this module's real hash-sharded legacy layout.
_POINT_ID = "aaaa1111"
_SHARD_LEVEL_1_END = 2
_SHARD_LEVEL_2_END = 4


class _FakeGoldenRepoManager:
    """Test double (not the SUT) -- controlled stand-in for the minimal
    golden_repo_manager surface enumerate_fleet_migration_candidates()
    needs, mirroring test_scheduler_1458.py's own established convention."""

    def __init__(self, repos: Dict[str, Path]):
        self._repos = repos

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])


class _RecordingConfigService:
    def __init__(self, *, enabled: bool = True, tick_interval_minutes: int = 30):
        self.enabled = enabled
        self.tick_interval_minutes = tick_interval_minutes
        self.canary_gate_enabled = False

    def get_config(self):
        cfg = self

        class _Wrapper:
            fleet_migration_config = cfg

        return _Wrapper()


class _ProgressRecorder:
    """Test double capturing every progress_callback invocation IN ORDER
    -- NOT a mock of anything under test, a plain recording collaborator."""

    def __init__(self) -> None:
        self.calls: List[Tuple[int, Optional[str], Optional[str]]] = []

    def __call__(
        self, progress: int, phase: Optional[str] = None, detail: Optional[str] = None
    ) -> None:
        self.calls.append((progress, phase, detail))


def _make_refresh_scheduler(tmp_path: Path) -> RefreshScheduler:
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

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


def _build_unconsolidated_repo(golden_repos_dir: Path, alias: str) -> Path:
    """A real golden repo base clone with ONE unconsolidated semantic
    collection (sharded vector_*.json, no chunks_db discriminator)."""
    base_clone = golden_repos_dir / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": _SAMPLE_VECTOR_DIMENSION})
    )
    shard_dir = (
        collection_dir
        / _POINT_ID[:_SHARD_LEVEL_1_END]
        / _POINT_ID[_SHARD_LEVEL_1_END:_SHARD_LEVEL_2_END]
    )
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": _POINT_ID,
        "vector": _SAMPLE_VECTOR,
        "metadata": {},
        "payload": {"path": "src/a.py"},
        "chunk_text": "x",
    }
    (shard_dir / f"vector_{_POINT_ID}.json").write_text(json.dumps(record))
    return base_clone


def _make_scheduler(
    tmp_path: Path,
    golden_repo_manager,
    refresh_scheduler,
    *,
    config_service=None,
) -> FleetMigrationScheduler:
    from code_indexer.server.services.config_service import set_config_service

    resolved_config_service = config_service or _RecordingConfigService()
    set_config_service(resolved_config_service)
    return FleetMigrationScheduler(
        golden_repo_manager=golden_repo_manager,
        refresh_scheduler=refresh_scheduler,
        background_job_manager=None,
        config_service=resolved_config_service,
    )


class TestRunNextCandidateDeclaresProgressCallback:
    def test_declares_progress_callback_parameter(self) -> None:
        """Bug #1562's literal root cause: BackgroundJobManager.submit_job()
        injects a progress_callback ONLY into a worker whose signature
        declares one -- see server/repositories/background_jobs.py's
        `"progress_callback" in func_signature.parameters` check."""
        signature = inspect.signature(FleetMigrationScheduler._run_next_candidate)
        assert "progress_callback" in signature.parameters, (
            "Bug #1562: FleetMigrationScheduler._run_next_candidate does "
            "not declare a progress_callback parameter -- "
            "BackgroundJobManager will never inject one, and the job will "
            "be pinned at a hardcoded progress value for its entire "
            "lifetime, exactly as observed on staging."
        )


class TestRunNextCandidateForwardsProgressCallback:
    def test_forwards_progress_callback_to_real_migration(self, tmp_path: Path) -> None:
        from code_indexer.server.services.config_service import (
            reset_config_service,
        )

        reset_config_service()
        try:
            refresh_scheduler = _make_refresh_scheduler(tmp_path)
            golden_repos_dir = tmp_path / "golden-repos"
            base_clone = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
            golden = _FakeGoldenRepoManager({"repo-a": base_clone})

            scheduler = _make_scheduler(
                tmp_path,
                golden,
                refresh_scheduler,
                config_service=_RecordingConfigService(enabled=True),
            )
            recorder = _ProgressRecorder()

            result = scheduler._run_next_candidate(progress_callback=recorder)

            collection_dir = (
                base_clone / ".code-indexer" / "index" / "semantic_collection"
            )
            assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB
            assert result["status"] == "completed"
            assert recorder.calls, (
                "Bug #1562: _run_next_candidate() completed a real "
                "migration but never invoked the progress_callback it was "
                "given -- the parameter exists but is not wired through "
                "to run_fleet_migration_for_repo()."
            )
            phases_seen = {phase.lower() for _p, phase, _d in recorder.calls if phase}
            assert any("writ" in phase for phase in phases_seen), (
                f"Bug #1562: no write-phase checkpoint was ever forwarded "
                f"all the way up through the scheduler -- phases seen: "
                f"{sorted(phases_seen)}"
            )
        finally:
            reset_config_service()
