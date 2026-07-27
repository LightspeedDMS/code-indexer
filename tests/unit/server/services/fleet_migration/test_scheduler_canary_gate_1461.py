"""Tests for FleetMigrationScheduler's proactive cross-repo canary gate
(Story #1461 salvage item #8).

`_run_next_candidate()` already has a REACTIVE consecutive-failure
quarantine breaker (Issue #1477, `quarantine.py`) that only reacts AFTER
the SAME repo fails repeatedly -- a systemic converter defect could touch
multiple DIFFERENT repos before that breaker ever notices. This adds a
PROACTIVE gate: when `fleet_migration_config.canary_gate_enabled` is True,
the fleet-wide sweep holds after the very first repo of a fresh sweep
migrates, pending an explicit admin confirmation
(`FleetMigrationScheduler.confirm_canary()` /
`trigger_now(confirm_canary=True)`), before a second repo is ever touched.

Fixtures duplicated from test_scheduler_1458.py (this project's own
established per-file-duplication convention in this test directory, rather
than a shared conftest -- see test_quarantine_1477.py for the same
pattern). Real RefreshScheduler, real GoldenRepoMetadataSqliteBackend (the
SAME backend quarantine.py's own tests use for the fleet-migration
quarantine state table, which the canary marker durably shares via a
sentinel alias), real filesystem-backed golden repo clones. Zero mocking
of the gate logic itself.
"""

import json
import uuid
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.server.services.config_service import (
    reset_config_service,
    set_config_service,
)
from code_indexer.server.services.fleet_migration.scheduler import (
    FleetMigrationScheduler,
)
from code_indexer.server.services.job_tracker import (
    DuplicateJobError as TrackerDuplicateJobError,
    JobTracker,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)


class _FakeGoldenRepoManager:
    """Test double (not the SUT) -- minimal golden_repo_manager surface,
    mirroring test_scheduler_1458.py's own fixture exactly."""

    def __init__(self, repos: Dict[str, Path], sqlite_backend=None):
        self._repos = repos
        self._sqlite_backend = sqlite_backend

    def list_golden_repos(self) -> List[Dict[str, str]]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])


class _RecordingConfigService:
    def __init__(
        self,
        *,
        enabled: bool = True,
        tick_interval_minutes: int = 30,
        canary_gate_enabled: bool = False,
    ):
        self.enabled = enabled
        self.tick_interval_minutes = tick_interval_minutes
        self.canary_gate_enabled = canary_gate_enabled

    def get_config(self):
        cfg = self

        class _Wrapper:
            fleet_migration_config = cfg

        return _Wrapper()


class _RealGateBackgroundJobManager:
    """submit_job() delegates into a REAL JobTracker instance."""

    def __init__(self, job_tracker: JobTracker):
        self._job_tracker = job_tracker

    def submit_job(
        self,
        operation_type: str,
        func,
        *args,
        submitter_username: str,
        is_admin: bool = False,
        repo_alias=None,
        **kwargs,
    ) -> str:
        job_id = str(uuid.uuid4())
        try:
            self._job_tracker.register_job_if_no_conflict(
                job_id=job_id,
                operation_type=operation_type,
                username=submitter_username,
                repo_alias=repo_alias,
                is_admin=is_admin,
            )
        except TrackerDuplicateJobError as exc:
            raise DuplicateJobError(
                exc.operation_type, exc.repo_alias, exc.existing_job_id
            ) from exc
        result = func(*args, **kwargs)
        self._job_tracker.complete_job(job_id, result=result)
        return job_id


@pytest.fixture(autouse=True)
def _reset_config_service_singleton():
    """See test_scheduler_1458.py's identical fixture docstring: the
    orchestrator independently resolves its Story #1460 rollout-safety gate
    from the global get_config_service() singleton -- reset around every
    test so this file's fakes never leak into a sibling test module."""
    reset_config_service()
    yield
    reset_config_service()


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


def _write_vector_json(collection_dir: Path, point_id: str, vector) -> None:
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {},
        "payload": {"path": "src/a.py"},
        "chunk_text": "x",
    }
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _build_unconsolidated_repo(golden_repos_dir: Path, alias: str) -> Path:
    """A real golden repo base clone with ONE unconsolidated semantic
    collection (sharded vector_*.json, no chunks_db discriminator)."""
    base_clone = golden_repos_dir / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 2})
    )
    _write_vector_json(collection_dir, "aaaa1111", [0.1, 0.2])
    return base_clone


def _make_scheduler(
    golden_repo_manager,
    refresh_scheduler,
    *,
    background_job_manager,
    config_service=None,
) -> FleetMigrationScheduler:
    resolved_config_service = config_service or _RecordingConfigService()
    set_config_service(resolved_config_service)
    return FleetMigrationScheduler(
        golden_repo_manager=golden_repo_manager,
        refresh_scheduler=refresh_scheduler,
        background_job_manager=background_job_manager,
        config_service=resolved_config_service,
    )


def _make_backend(tmp_path: Path) -> GoldenRepoMetadataSqliteBackend:
    db_path = str(tmp_path / "golden_repo_metadata.db")
    backend = GoldenRepoMetadataSqliteBackend(db_path)
    backend.ensure_table_exists()
    return backend


def _collection_dir(base_clone: Path) -> Path:
    return base_clone / ".code-indexer" / "index" / "semantic_collection"


class TestCanaryGateHoldsSweepAfterFirstRepo:
    """Scenario A: with the gate enabled, the SECOND `_run_next_candidate()`
    call of a fresh sweep must hold (canary_pending) rather than touching a
    second repo, even though that second repo is genuinely pending."""

    def test_second_tick_returns_canary_pending_and_second_repo_is_untouched(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        repo_a_base = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        repo_b_base = _build_unconsolidated_repo(golden_repos_dir, "repo-b")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a": repo_a_base, "repo-b": repo_b_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(
                enabled=True, canary_gate_enabled=True
            ),
        )

        tick_1 = scheduler._run_next_candidate()

        assert tick_1["status"] == "completed"
        assert tick_1["golden_alias"] == "repo-a"
        assert resolve_chunk_layout(_collection_dir(repo_a_base)) == (
            ChunkLayout.CHUNKS_DB
        )

        tick_2 = scheduler._run_next_candidate()

        assert tick_2["status"] == "canary_pending"
        assert tick_2["golden_alias"] == "repo-a"
        # repo-b must remain completely untouched -- the gate held the
        # sweep BEFORE the destructive orchestrator was ever invoked for
        # it.
        assert resolve_chunk_layout(_collection_dir(repo_b_base)) == (
            ChunkLayout.SHARDED_JSON
        )


class TestConfirmCanaryUnblocksTheSecondRepo:
    """Scenario B: an explicit admin confirmation clears the pending
    marker, letting the very next tick migrate the second repo."""

    def test_confirm_canary_then_next_tick_migrates_second_repo(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        repo_a_base = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        repo_b_base = _build_unconsolidated_repo(golden_repos_dir, "repo-b")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a": repo_a_base, "repo-b": repo_b_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(
                enabled=True, canary_gate_enabled=True
            ),
        )

        tick_1 = scheduler._run_next_candidate()
        assert tick_1["status"] == "completed"
        tick_2 = scheduler._run_next_candidate()
        assert tick_2["status"] == "canary_pending"
        # Sanity: repo-b genuinely still pending before confirmation.
        assert resolve_chunk_layout(_collection_dir(repo_b_base)) == (
            ChunkLayout.SHARDED_JSON
        )

        scheduler.confirm_canary()
        tick_3 = scheduler._run_next_candidate()

        assert tick_3["status"] == "completed"
        assert tick_3["golden_alias"] == "repo-b"
        assert resolve_chunk_layout(_collection_dir(repo_b_base)) == (
            ChunkLayout.CHUNKS_DB
        )

    def test_confirm_canary_via_trigger_now_kwarg(self, tmp_path: Path) -> None:
        """trigger_now(confirm_canary=True) must ALSO clear the marker,
        via the real BackgroundJobManager-integrated entry point (not just
        the bare confirm_canary() method)."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        repo_a_base = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        repo_b_base = _build_unconsolidated_repo(golden_repos_dir, "repo-b")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a": repo_a_base, "repo-b": repo_b_base},
            sqlite_backend=backend,
        )
        db_path = str(tmp_path / "cidx_server.db")
        DatabaseSchema(db_path).initialize_database()
        job_tracker = JobTracker(db_path)
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)
        scheduler = _make_scheduler(
            golden,
            refresh_scheduler,
            background_job_manager=bg_job_manager,
            config_service=_RecordingConfigService(
                enabled=True, canary_gate_enabled=True
            ),
        )

        # tick 1: migrates repo-a and records the pending canary marker.
        job_id_1 = scheduler.trigger_now()
        assert job_id_1 is not None
        tracked_1 = job_tracker.get_job(job_id_1)
        assert tracked_1.result["status"] == "completed"

        # tick 2 (no confirmation): held.
        job_id_2 = scheduler.trigger_now()
        assert job_id_2 is not None
        tracked_2 = job_tracker.get_job(job_id_2)
        assert tracked_2.result["status"] == "canary_pending"
        assert resolve_chunk_layout(_collection_dir(repo_b_base)) == (
            ChunkLayout.SHARDED_JSON
        )

        # tick 3, confirming inline via the kwarg: unblocks repo-b.
        job_id_3 = scheduler.trigger_now(confirm_canary=True)
        assert job_id_3 is not None
        tracked_3 = job_tracker.get_job(job_id_3)
        assert tracked_3.result["status"] == "completed"
        assert tracked_3.result["golden_alias"] == "repo-b"
        assert resolve_chunk_layout(_collection_dir(repo_b_base)) == (
            ChunkLayout.CHUNKS_DB
        )


class TestCanaryGateDisabledByDefaultIsByteIdentical:
    """Scenario C: with canary_gate_enabled=False (the default), two
    consecutive ticks must migrate BOTH repos with no canary_pending ever
    appearing -- proving byte-identical pre-#1461 behavior."""

    def test_both_repos_migrate_across_two_ticks_with_gate_disabled(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        repo_a_base = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        repo_b_base = _build_unconsolidated_repo(golden_repos_dir, "repo-b")
        backend = _make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"repo-a": repo_a_base, "repo-b": repo_b_base},
            sqlite_backend=backend,
        )
        # _RecordingConfigService()'s canary_gate_enabled defaults to
        # False -- the exact production default (FleetMigrationConfig).
        scheduler = _make_scheduler(
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        tick_1 = scheduler._run_next_candidate()
        tick_2 = scheduler._run_next_candidate()

        assert tick_1["status"] == "completed"
        assert tick_1["golden_alias"] == "repo-a"
        assert tick_2["status"] == "completed"
        assert tick_2["golden_alias"] == "repo-b"
        assert "canary_pending" not in (tick_1["status"], tick_2["status"])
        assert resolve_chunk_layout(_collection_dir(repo_a_base)) == (
            ChunkLayout.CHUNKS_DB
        )
        assert resolve_chunk_layout(_collection_dir(repo_b_base)) == (
            ChunkLayout.CHUNKS_DB
        )


class _AlwaysFailingCanaryReadBackend:
    """Simulates a PERSISTENT backend read failure for the canary marker's
    underlying get_fleet_migration_failure_state() call -- mirrors
    test_scheduler_1458.py's own _AlwaysFailingQuarantineBackend."""

    def get_fleet_migration_failure_state(self, golden_alias):
        raise RuntimeError("simulated persistent backend outage")


class TestCanaryGateBackendReadFailureAbortsTheTick:
    """Scenario D: a genuine backend read failure while checking for a
    pending canary marker must abort the tick with a distinct status,
    never silently proceed as if no canary were pending (which would
    defeat the whole gate during a backend outage)."""

    def test_persistent_read_failure_aborts_without_migrating(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        repo_a_base = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager(
            {"repo-a": repo_a_base},
            sqlite_backend=_AlwaysFailingCanaryReadBackend(),
        )
        scheduler = _make_scheduler(
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(
                enabled=True, canary_gate_enabled=True
            ),
        )

        result = scheduler._run_next_candidate()

        assert result["status"] == "quarantine_state_unavailable"
        # Migration must never have run while the canary-marker state was
        # indeterminate.
        assert resolve_chunk_layout(_collection_dir(repo_a_base)) == (
            ChunkLayout.SHARDED_JSON
        )
