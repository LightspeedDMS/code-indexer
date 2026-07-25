"""Tests for FleetMigrationScheduler (Story #1458, Background Jobs
Checklist: "Migration job integrates with BackgroundJobManager + JobTracker
for dashboard/admin UI visibility").

`run_fleet_migration_for_repo()` is real and tested (test_orchestrator_1458.py)
but was not wired to any scheduler/admin surface -- this is that missing
link. Real RefreshScheduler (the SAME `_make_scheduler` helper
test_orchestrator_1458.py establishes), real JobTracker backed by a real
SQLite `background_jobs` table (via DatabaseSchema.initialize_database()),
and a `_RealGateBackgroundJobManager` test double that delegates
`submit_job()` straight into that real JobTracker.register_job_if_no_conflict
-- the IDENTICAL, established pattern
tests/unit/server/services/hnsw_orphan_sweep/test_scheduler_1360.py already
uses -- so the fleet-wide single-flight dedup gate is proven against the
real DB-level `idx_active_job_per_repo` unique-index constraint, never a
mock of it (feedback_faithful_db_mocks).

Real filesystem collections (vector_*.json shards + collection_meta.json),
real chunk_layout resolution -- migration itself genuinely runs end-to-end
via the real orchestrator, not a stub.
"""

import json
import threading
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
from code_indexer.server.services.fleet_migration.completion_gate import (
    mark_post_consolidation_snapshot_published,
    repo_has_published_post_consolidation_snapshot,
)
from code_indexer.server.services.fleet_migration.discovery import (
    enumerate_fleet_migration_candidates,
    is_repo_already_migrated,
)
from code_indexer.server.services.fleet_migration.scheduler import (
    FleetMigrationScheduler,
)
from code_indexer.server.services.job_tracker import (
    DuplicateJobError as TrackerDuplicateJobError,
    JobTracker,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)


class _FakeGoldenRepoManager:
    """Test double (not the SUT) -- controlled stand-in for the minimal
    golden_repo_manager surface enumerate_fleet_migration_candidates()
    needs, mirroring hnsw_orphan_sweep's own test convention."""

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

    def get_config(self):
        cfg = self

        class _Wrapper:
            fleet_migration_config = cfg

        return _Wrapper()


class _RealGateBackgroundJobManager:
    """submit_job() delegates into a REAL JobTracker instance -- exercising
    the actual idx_active_job_per_repo DB-level gate, not a mock of it."""

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
        # Execute synchronously for test determinism (no real thread pool).
        result = func(*args, **kwargs)
        self._job_tracker.complete_job(job_id, result=result)
        return job_id


@pytest.fixture
def job_tracker(tmp_path: Path) -> JobTracker:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return JobTracker(db_path)


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


def _build_already_migrated_repo(golden_repos_dir: Path, alias: str) -> Path:
    """A real golden repo base clone whose only collection is already
    consolidated, produced via the REAL consolidate_collection_in_place()
    flow (not hand-constructed) so it automatically carries whatever real
    migrated state requires -- genuine chunks.db, discriminator, zero
    remaining legacy files, AND (Codex CRITICAL finding round 4) the
    crash-durable content-integrity manifest -- and has zero residual
    temporal dirs."""
    from code_indexer.storage.shared.collection_migration import (
        consolidate_collection_in_place,
    )

    base_clone = golden_repos_dir / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 2})
    )
    shard_dir = collection_dir / "mi" / "gr"
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "migrated1",
        "vector": [0.1, 0.2],
        "metadata": {},
        "payload": {"path": "src/a.py"},
        "chunk_text": "migrated",
    }
    (shard_dir / "vector_migrated1.json").write_text(json.dumps(record))

    result = consolidate_collection_in_place(collection_dir)
    assert result.status == "consolidated"
    mark_post_consolidation_snapshot_published(index_path)
    return base_clone


def _make_scheduler(
    tmp_path: Path,
    golden_repo_manager,
    refresh_scheduler,
    *,
    background_job_manager,
    config_service=None,
) -> FleetMigrationScheduler:
    return FleetMigrationScheduler(
        golden_repo_manager=golden_repo_manager,
        refresh_scheduler=refresh_scheduler,
        background_job_manager=background_job_manager,
        config_service=config_service or _RecordingConfigService(),
    )


class TestTriggerNowMigratesTheNextUnmigratedRepo:
    def test_migrates_the_only_repo_via_the_real_orchestrator(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        job_id = scheduler.trigger_now()

        assert job_id is not None
        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB

    def test_skips_already_migrated_repo_and_migrates_the_next_one(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        migrated_base = _build_already_migrated_repo(golden_repos_dir, "repo-a-done")
        pending_base = _build_unconsolidated_repo(golden_repos_dir, "repo-b-pending")
        golden = _FakeGoldenRepoManager(
            {"repo-a-done": migrated_base, "repo-b-pending": pending_base}
        )
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        scheduler.trigger_now()

        pending_collection = (
            pending_base / ".code-indexer" / "index" / "semantic_collection"
        )
        assert resolve_chunk_layout(pending_collection) == ChunkLayout.CHUNKS_DB

    def test_returns_nothing_to_migrate_status_when_fleet_is_fully_migrated(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_already_migrated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        job_id = scheduler.trigger_now()

        assert job_id is not None
        tracked_job = job_tracker.get_job(job_id)
        assert tracked_job is not None
        assert tracked_job.result is not None
        assert tracked_job.result["status"] == "nothing_to_migrate"


class TestFleetWideSingleFlightDedup:
    def test_trigger_now_returns_none_when_a_migration_already_in_flight(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})

        # A real in-flight fleet_migration job already registered under the
        # scheduler's FIXED sentinel repo_alias -- proves fleet-wide
        # single-flight (not merely per-repo) against the REAL DB-level
        # idx_active_job_per_repo unique-index gate.
        job_tracker.register_job_if_no_conflict(
            job_id=str(uuid.uuid4()),
            operation_type=FleetMigrationScheduler.OPERATION_TYPE,
            username="system",
            repo_alias=FleetMigrationScheduler._SCHEDULER_REPO_ALIAS,
            is_admin=True,
        )
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        job_id = scheduler.trigger_now()

        assert job_id is None
        # The already-unconsolidated repo must be untouched -- no migration
        # ran because the fixed-sentinel dedup gate rejected the submission.
        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON


class TestGetStats:
    def test_reports_total_migrated_and_pending_repo_counts(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        _build_already_migrated_repo(golden_repos_dir, "repo-a-done")
        _build_unconsolidated_repo(golden_repos_dir, "repo-b-pending")
        _build_unconsolidated_repo(golden_repos_dir, "repo-c-pending")
        golden = _FakeGoldenRepoManager(
            {
                "repo-a-done": golden_repos_dir / "repo-a-done",
                "repo-b-pending": golden_repos_dir / "repo-b-pending",
                "repo-c-pending": golden_repos_dir / "repo-c-pending",
            }
        )
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)
        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        stats = scheduler.get_stats()

        assert stats == {
            "total_repos": 3,
            "migrated_repos": 1,
            "pending_repos": 2,
        }


class TestKillSwitchGate:
    """Codex Finding #6 (CRITICAL, HIGH severity): only _loop() checked the
    enabled flag -- trigger_now() and _run_next_candidate() migrated
    destructively with NO kill-switch check at all. Both entry points must
    independently refuse when fleet_migration_config.enabled=False, since
    either can be reached without ever going through _loop() (a manual
    admin trigger, or a job-queue worker invoking the submitted callable
    directly)."""

    def test_trigger_now_refuses_when_disabled(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=bg_job_manager,
            config_service=_RecordingConfigService(enabled=False),
        )

        job_id = scheduler.trigger_now()

        assert job_id is None, (
            "Bug: trigger_now() submitted a real migration job while "
            "fleet_migration_config.enabled=False -- the kill switch was "
            "bypassed entirely on this entry point."
        )
        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON, (
            "Bug: the repo was migrated for real despite the kill switch "
            "being disabled."
        )

    def test_run_next_candidate_refuses_when_disabled_even_when_called_directly(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        """The kill switch must be checked independently at THIS entry
        point too -- a job-queue worker (or any future caller) invoking the
        submitted callable directly, bypassing trigger_now()'s own check
        entirely, must still be refused."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=bg_job_manager,
            config_service=_RecordingConfigService(enabled=False),
        )

        result = scheduler._run_next_candidate()

        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON, (
            "Bug: _run_next_candidate() migrated a real repo despite the "
            "kill switch being disabled -- called directly, bypassing "
            "trigger_now()'s own check."
        )
        assert result["status"] == "disabled"

    def test_run_next_candidate_migrates_when_enabled(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        """Sanity: the gate must not be a blanket refusal -- an explicitly
        enabled config must still allow real migration to proceed."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=bg_job_manager,
            config_service=_RecordingConfigService(enabled=True),
        )

        result = scheduler._run_next_candidate()

        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB
        assert result["status"] == "completed"


class TestMarkerStalenessAcrossMigrationGenerations:
    """Codex CRITICAL finding (round 4): the snapshot-published marker is
    a permanent file with no generation identity. Scenario: generation A
    succeeds (marker written). Later, NEW unconsolidated data appears
    (e.g. ongoing indexing). Generation B consolidates it successfully but
    crashes BEFORE its own snapshot fires -- the STALE marker from A must
    never make is_repo_already_migrated() report the repo as migrated,
    permanently hiding that B's required snapshot never happened."""

    def test_stale_marker_from_prior_generation_does_not_mask_a_failed_new_snapshot(
        self, tmp_path: Path, job_tracker: JobTracker, monkeypatch
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"

        # Generation A: fully migrated via the real flow, marker present.
        base_clone = _build_already_migrated_repo(golden_repos_dir, "repo-a")
        index_path = base_clone / ".code-indexer" / "index"
        assert repo_has_published_post_consolidation_snapshot(index_path) is True

        # NEW unconsolidated collection appears (e.g. ongoing indexing
        # activity added a second collection after generation A published).
        new_collection_dir = index_path / "new_collection"
        new_collection_dir.mkdir(parents=True)
        (new_collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "new_collection", "vector_size": 2})
        )
        _write_vector_json(new_collection_dir, "newpoint1", [0.5, 0.5])

        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)
        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        # Simulate generation B crashing right at its own snapshot trigger
        # -- AFTER the new collection has already been consolidated.
        import code_indexer.server.services.fleet_migration.orchestrator as orch_mod

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash during snapshot trigger")

        monkeypatch.setattr(orch_mod, "trigger_post_consolidation_snapshot", _boom)

        with pytest.raises(RuntimeError):
            scheduler.trigger_now()

        # The new collection WAS genuinely consolidated before the crash.
        assert resolve_chunk_layout(new_collection_dir) == ChunkLayout.CHUNKS_DB

        candidate = next(iter(enumerate_fleet_migration_candidates(golden)))
        assert is_repo_already_migrated(candidate) is False, (
            "Bug: the STALE marker from generation A was mistaken for "
            "generation B's completion, even though B's own snapshot "
            "never fired -- this repo will NEVER be revisited by the "
            "scheduler again."
        )


# Codex round-6 MEDIUM finding (scheduler shutdown) test constants -- named
# to avoid magic numbers in TestStopChecksThreadLivenessAfterJoin.
SHORT_JOIN_TIMEOUT_SECONDS = 0.05
STUCK_THREAD_WAIT_SECONDS = 5
POLITE_POLL_INTERVAL_SECONDS = 0.01


class TestStopChecksThreadLivenessAfterJoin:
    """Codex round-6 MEDIUM finding: stop() does a 10-second timed join
    but never actually checks whether the worker thread is STILL ALIVE
    afterward. A real repro confirmed the thread was still alive after
    stop() returned -- silently reported as success. Fix: check
    thread.is_alive() after the join and raise/log loudly if it did not
    actually stop."""

    def _make_minimal_scheduler(self) -> FleetMigrationScheduler:
        return FleetMigrationScheduler(
            golden_repo_manager=MagicMock(),
            refresh_scheduler=MagicMock(),
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(),
        )

    def test_stop_raises_when_worker_thread_does_not_actually_stop(
        self, monkeypatch
    ) -> None:
        import code_indexer.server.services.fleet_migration.scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod, "_STOP_JOIN_TIMEOUT_SECONDS", SHORT_JOIN_TIMEOUT_SECONDS
        )

        scheduler = self._make_minimal_scheduler()
        release_event = threading.Event()

        def _stuck_worker():
            # Deliberately ignores stop_event -- simulates a genuinely
            # hung/stuck worker thread that does not stop within the
            # bounded join window.
            release_event.wait(timeout=STUCK_THREAD_WAIT_SECONDS)

        stuck_thread = threading.Thread(
            target=_stuck_worker, daemon=True, name="stuck-test-thread"
        )
        stuck_thread.start()
        scheduler._thread = stuck_thread

        try:
            with pytest.raises(RuntimeError):
                scheduler.stop()
        finally:
            release_event.set()
            stuck_thread.join(timeout=STUCK_THREAD_WAIT_SECONDS)

    def test_stop_does_not_raise_when_the_thread_genuinely_stops(self) -> None:
        scheduler = self._make_minimal_scheduler()

        def _well_behaved_worker():
            while not scheduler._stop_event.is_set():
                scheduler._stop_event.wait(timeout=POLITE_POLL_INTERVAL_SECONDS)

        real_thread = threading.Thread(
            target=_well_behaved_worker, daemon=True, name="well-behaved-test-thread"
        )
        real_thread.start()
        scheduler._thread = real_thread

        scheduler.stop()  # must not raise

        assert real_thread.is_alive() is False


class TestReadCycleConfigValidatesTickInterval:
    """Codex round-6 MEDIUM finding: _read_cycle_config() accepted
    zero/negative tick_interval_minutes unvalidated -- in _loop(), this
    produces wait_seconds <= 0, and the inner bounded-wait loop's
    condition (elapsed < wait_seconds) is immediately False, so the
    scheduler never sleeps at all: a continuous busy-spin re-submitting
    migration jobs with zero delay between attempts. Fix: validate a
    positive, finite, sane interval when reading config."""

    def _make_scheduler_with_interval(
        self, tick_interval_minutes: int
    ) -> FleetMigrationScheduler:
        return FleetMigrationScheduler(
            golden_repo_manager=MagicMock(),
            refresh_scheduler=MagicMock(),
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(
                tick_interval_minutes=tick_interval_minutes
            ),
        )

    @pytest.mark.parametrize("invalid_interval", [0, -5])
    def test_non_positive_tick_interval_is_not_forwarded_as_is(
        self, invalid_interval: int
    ) -> None:
        scheduler = self._make_scheduler_with_interval(invalid_interval)

        try:
            cycle_cfg = scheduler._read_cycle_config()

            assert cycle_cfg["interval_minutes"] > 0, (
                f"Bug: tick_interval_minutes={invalid_interval} was "
                f"forwarded unvalidated -- this produces "
                f"wait_seconds<=0 in _loop(), causing a continuous "
                f"busy-spin with zero delay between migration job "
                f"submissions."
            )
        finally:
            scheduler.stop()  # no-op: .start() was never called
