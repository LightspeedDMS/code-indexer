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
from code_indexer.server.services.config_service import (
    reset_config_service,
    set_config_service,
)
from code_indexer.server.services.fleet_migration.completion_gate import (
    mark_post_consolidation_snapshot_published,
    repo_has_published_post_consolidation_snapshot,
)
from code_indexer.server.services.fleet_migration.discovery import (
    enumerate_fleet_migration_candidates,
    is_repo_already_migrated,
)
from code_indexer.server.services.fleet_migration.orchestrator import (
    FleetMigrationRepoResult,
)
from code_indexer.server.services.fleet_migration.quarantine import (
    FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD,
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
    """Test double (not the SUT) -- controlled stand-in for the minimal
    golden_repo_manager surface enumerate_fleet_migration_candidates()
    needs, mirroring hnsw_orphan_sweep's own test convention.

    Issue #1477: an optional ``sqlite_backend`` (a REAL
    GoldenRepoMetadataSqliteBackend, never a mock) may be supplied, stored
    under the SAME ``_sqlite_backend`` attribute name the real
    GoldenRepoManager uses -- this is the exact attribute
    quarantine.py's ``_get_quarantine_backend()`` reuses (mirroring
    golden_repo_reconciler.py's own ``_get_breaker_backend()`` convention).
    Defaults to None so every pre-existing test in this file (which never
    exercises quarantine) is unaffected.
    """

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


@pytest.fixture(autouse=True)
def _reset_config_service_singleton():
    """Story #1460: run_fleet_migration_for_repo() now independently
    resolves its deletion_authorized rollout-safety gate from the global
    get_config_service() singleton when the scheduler doesn't override it
    -- _make_scheduler() registers the SAME config_service object into
    that singleton (mirroring lifespan.py's real production wiring, where
    both the scheduler and the orchestrator's default resolution share
    literally one ConfigService instance). Reset around every test so
    this file's fakes never leak into a sibling test module."""
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


def _build_corrupt_repo_with_duplicate_point_id(
    golden_repos_dir: Path, alias: str
) -> Path:
    """A real golden repo base clone whose semantic collection has TWO
    vector_*.json files sharing the SAME point 'id' in different
    hash-shard subdirectories -- genuine pre-existing data corruption that
    scan_vectors_for_id_map correctly refuses to auto-resolve (Messi Rule
    #13 Anti-Silent-Failure), reproducing the exact real-world Issue #1477
    failure mode ("click" repo failing identically on every tick)."""
    base_clone = golden_repos_dir / alias
    index_path = base_clone / ".code-indexer" / "index"
    collection_dir = index_path / "semantic_collection"
    collection_dir.mkdir(parents=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 2})
    )
    duplicate_id = "dupe0001"
    record = {
        "id": duplicate_id,
        "vector": [0.1, 0.2],
        "metadata": {},
        "payload": {"path": "src/a.py"},
        "chunk_text": "x",
    }
    shard_a = collection_dir / "aa" / "bb"
    shard_a.mkdir(parents=True, exist_ok=True)
    (shard_a / f"vector_{duplicate_id}_a.json").write_text(json.dumps(record))
    shard_b = collection_dir / "cc" / "dd"
    shard_b.mkdir(parents=True, exist_ok=True)
    (shard_b / f"vector_{duplicate_id}_b.json").write_text(json.dumps(record))
    return base_clone


def _make_scheduler(
    tmp_path: Path,
    golden_repo_manager,
    refresh_scheduler,
    *,
    background_job_manager,
    config_service=None,
) -> FleetMigrationScheduler:
    resolved_config_service = config_service or _RecordingConfigService()
    # Mirror lifespan.py's real production wiring: the scheduler AND
    # run_fleet_migration_for_repo()'s Story #1460 default deletion
    # -authorized resolution must observe the SAME config_service, or the
    # scheduler's own kill-switch check and the orchestrator's independent
    # rollout-safety-gate resolution can silently disagree in a test.
    set_config_service(resolved_config_service)
    return FleetMigrationScheduler(
        golden_repo_manager=golden_repo_manager,
        refresh_scheduler=refresh_scheduler,
        background_job_manager=background_job_manager,
        config_service=resolved_config_service,
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

    def test_trigger_now_submits_no_job_when_fleet_is_fully_migrated(
        self, tmp_path: Path, job_tracker: JobTracker
    ) -> None:
        """Bug #1486 Fix C item 1 (auto-stop): once every repo is
        migrated, trigger_now() must go dormant -- refusing to submit
        ANY job -- instead of the pre-fix behavior of submitting a
        no-op "nothing_to_migrate" job on every single tick forever (a
        confirmed production incident: 3000+ jobs/day at a 1-minute
        tick interval)."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_already_migrated_repo(golden_repos_dir, "repo-a")
        golden = _FakeGoldenRepoManager({"repo-a": base_clone})
        bg_job_manager = _RealGateBackgroundJobManager(job_tracker)

        scheduler = _make_scheduler(
            tmp_path, golden, refresh_scheduler, background_job_manager=bg_job_manager
        )

        job_id = scheduler.trigger_now()

        assert job_id is None, (
            "Bug: trigger_now() submitted a job even though the fleet is "
            "fully migrated -- it must go dormant instead of creating a "
            "no-op job every tick forever."
        )


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
            "quarantined_repos": 0,
            "unrecoverable_repos": 0,
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


class TestFleetMigrationFailureQuarantine:
    """Issue #1477: a golden repo whose migration throws every single time
    (genuinely corrupt legacy data `scan_vectors_for_id_map` correctly
    refuses to auto-resolve) must NOT be retried forever, permanently
    starving every alphabetically-later repo in the fleet -- this is the
    exact live-observed incident ("click" failing every tick, "evolution"
    never reached).

    Real corrupt on-disk data (two vector_*.json files sharing the same
    point 'id'), real GoldenRepoMetadataSqliteBackend persistence -- no
    mocking of the scheduler/orchestrator/quarantine logic itself.
    """

    def _make_backend(self, tmp_path: Path) -> GoldenRepoMetadataSqliteBackend:
        db_path = str(tmp_path / "golden_repo_metadata.db")
        backend = GoldenRepoMetadataSqliteBackend(db_path)
        backend.ensure_table_exists()
        return backend

    def test_below_threshold_the_same_corrupt_repo_is_attempted_every_call(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD - 1):
            result = scheduler._run_next_candidate()
            assert result["status"] == "dedup_gate_rejected"

        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None
        assert (
            state["consecutive_failure_count"]
            == FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD - 1
        )

    def test_repo_is_quarantined_and_scheduler_advances_to_next_candidate(
        self, tmp_path: Path
    ) -> None:
        """The CORE fix: once the corrupt repo reaches the quarantine
        threshold, the scheduler must skip it and migrate the next
        alias-sorted candidate instead of raising the same error
        forever."""
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        pending_base = _build_unconsolidated_repo(golden_repos_dir, "evolution")
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"click": corrupt_base, "evolution": pending_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            result = scheduler._run_next_candidate()
            assert result["status"] == "dedup_gate_rejected"

        # "click" is now quarantined -- the NEXT call must skip it and
        # migrate "evolution" instead of raising the identical error again.
        result = scheduler._run_next_candidate()

        assert result["status"] == "completed"
        assert result["golden_alias"] == "evolution"
        evolution_collection = (
            pending_base / ".code-indexer" / "index" / "semantic_collection"
        )
        assert resolve_chunk_layout(evolution_collection) == ChunkLayout.CHUNKS_DB
        # The corrupt repo's own collection must remain untouched --
        # quarantine skips it, it does not "fix" or delete anything.
        corrupt_collection = (
            corrupt_base / ".code-indexer" / "index" / "semantic_collection"
        )
        assert resolve_chunk_layout(corrupt_collection) == ChunkLayout.SHARDED_JSON

    def test_success_resets_the_failure_counter(self, tmp_path: Path) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        # Two failures (below threshold).
        for _ in range(2):
            result = scheduler._run_next_candidate()
            assert result["status"] == "dedup_gate_rejected"
        assert (
            backend.get_fleet_migration_failure_state("click")[
                "consecutive_failure_count"
            ]
            == 2
        )

        # Operator remediation: remove the duplicate, leaving one valid
        # record -- the collection can now genuinely consolidate.
        collection_dir = (
            corrupt_base / ".code-indexer" / "index" / "semantic_collection"
        )
        (collection_dir / "cc" / "dd" / "vector_dupe0001_b.json").unlink()

        result = scheduler._run_next_candidate()

        assert result["status"] == "completed"
        assert backend.get_fleet_migration_failure_state("click") is None

    def test_quarantine_auto_clears_on_genuine_on_disk_state_change(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import code_indexer.server.services.fleet_migration.quarantine as quarantine_module

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            result = scheduler._run_next_candidate()
            assert result["status"] == "dedup_gate_rejected"

        # Quarantined: a further call with NO on-disk change returns
        # nothing_to_migrate (the only candidate is skipped) rather than
        # raising again.
        assert scheduler._run_next_candidate() == {"status": "nothing_to_migrate"}

        # Genuine on-disk state change (e.g. a partial operator remediation
        # attempt that still leaves the corruption unresolved) -- this must
        # auto-clear the quarantine and allow a retry, which fails again
        # for the SAME underlying reason.
        collection_dir = (
            corrupt_base / ".code-indexer" / "index" / "semantic_collection"
        )
        (collection_dir / "unrelated_new_file_added_by_operator").mkdir()

        # Finding C (Codex round-3 review): the scheduler always calls
        # is_quarantined() via its production default (no override), so
        # bypass the new throttle window here -- this test validates the
        # auto-clear DETECTION logic itself (immediately after a genuine
        # change), which is a SEPARATE concern from throttle timing
        # (covered by quarantine.py's own dedicated throttle tests).
        monkeypatch.setattr(quarantine_module, "_SIGNATURE_RECHECK_INTERVAL_SECONDS", 0)

        result = scheduler._run_next_candidate()
        assert result["status"] == "dedup_gate_rejected"

        # The retry's own failure was recorded fresh (count reset to 1,
        # not accumulated on top of the old quarantine-triggering count).
        assert (
            backend.get_fleet_migration_failure_state("click")[
                "consecutive_failure_count"
            ]
            == 1
        )

    def test_get_stats_reports_quarantined_repos_count(self, tmp_path: Path) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        pending_base = _build_unconsolidated_repo(golden_repos_dir, "evolution")
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"click": corrupt_base, "evolution": pending_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            result = scheduler._run_next_candidate()
            assert result["status"] == "dedup_gate_rejected"

        stats = scheduler.get_stats()

        assert stats["quarantined_repos"] == 1

    def test_quarantine_auto_clears_when_nested_shard_duplicate_is_removed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Finding 1 (dual code-review round): the auto-clear signature
        must be sensitive to NESTED shard-directory state, not just the
        top-level collection directory -- deleting the duplicate file deep
        inside a hash-shard subdirectory (the real operator remediation
        for this exact corruption) previously left the top-level dir's own
        mtime/entry-count unchanged, so the quarantine never cleared."""
        import code_indexer.server.services.fleet_migration.quarantine as quarantine_module

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            result = scheduler._run_next_candidate()
            assert result["status"] == "dedup_gate_rejected"

        # Quarantined -- confirm skip with no on-disk change.
        assert scheduler._run_next_candidate() == {"status": "nothing_to_migrate"}

        # Real operator remediation: delete ONE of the two nested
        # duplicate files (the genuine fix for this corruption), deep
        # inside a hash-shard subdirectory -- this must NOT be invisible
        # to the auto-clear signature.
        collection_dir = (
            corrupt_base / ".code-indexer" / "index" / "semantic_collection"
        )
        (collection_dir / "cc" / "dd" / "vector_dupe0001_b.json").unlink()

        # Finding C (Codex round-3 review): bypass the new throttle
        # window here -- this test validates NESTED-shard-change
        # detection itself (Finding 1), a separate concern from throttle
        # timing (covered by quarantine.py's own dedicated tests).
        monkeypatch.setattr(quarantine_module, "_SIGNATURE_RECHECK_INTERVAL_SECONDS", 0)

        result = scheduler._run_next_candidate()

        assert result["status"] == "completed"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB


class TestQuarantineCountsNonRaisingStatuses:
    """Finding 2 (Codex-found, reproduced concretely, dual review round):
    a repo whose migration returns a non-raising, no-progress status (e.g.
    "incomplete" from a persistent disk-space skip) was never recorded as
    a failure, never quarantined, and re-selected as the first pending
    candidate on EVERY subsequent tick forever -- the EXACT fleet-
    starvation bug #1477 reports, triggered via a non-exception path
    instead of an exception.

    Uses a monkeypatched `run_fleet_migration_for_repo` returning a canned
    `FleetMigrationRepoResult` -- isolating the SCHEDULER's status
    classification/quarantine-counting logic from the orchestrator's own
    (separately, already fully tested in test_orchestrator_1458.py)
    disk-headroom/lock-conflict plumbing.
    """

    def _make_backend(self, tmp_path: Path) -> GoldenRepoMetadataSqliteBackend:
        db_path = str(tmp_path / "golden_repo_metadata.db")
        backend = GoldenRepoMetadataSqliteBackend(db_path)
        backend.ensure_table_exists()
        return backend

    def test_repeated_incomplete_results_eventually_quarantine_and_advance(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import code_indexer.server.services.fleet_migration.scheduler as sched_mod

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        stuck_base = _build_unconsolidated_repo(golden_repos_dir, "click")
        pending_base = _build_unconsolidated_repo(golden_repos_dir, "evolution")
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager(
            {"click": stuck_base, "evolution": pending_base},
            sqlite_backend=backend,
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        def _fake_incomplete(*args, **kwargs):
            # Deliberately a GENERIC (not disk-headroom-specific) detail
            # -- this test validates that ANY non-progress "incomplete"
            # status counts toward quarantine and lets the queue
            # advance, independent of Finding I's disk-headroom-specific
            # auto-clear path (covered separately by
            # TestIsQuarantinedClearsDiskHeadroomCauseIndependently in
            # test_quarantine_1477.py). A disk-headroom-worded detail
            # here would trigger that INDEPENDENT auto-clear against a
            # repo with no real disk-space problem, which is a correct
            # but unrelated interaction this test does not intend to
            # exercise.
            return FleetMigrationRepoResult(
                status="incomplete",
                detail="residual in-repo temporal directories remain (simulated)",
            )

        monkeypatch.setattr(sched_mod, "run_fleet_migration_for_repo", _fake_incomplete)

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            result = scheduler._run_next_candidate()
            assert result["status"] == "incomplete"
            assert result["golden_alias"] == "click"

        state = backend.get_fleet_migration_failure_state("click")
        assert state is not None
        assert (
            state["consecutive_failure_count"]
            == FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD
        )

        # "click" is now quarantined -- restore the REAL orchestrator so
        # the next candidate ("evolution") can genuinely migrate.
        monkeypatch.undo()
        result = scheduler._run_next_candidate()

        assert result["status"] == "completed"
        assert result["golden_alias"] == "evolution"

    @pytest.mark.parametrize("transient_status", ["lock_held", "refresh_in_flight"])
    def test_transient_statuses_never_increment_the_failure_counter(
        self, tmp_path: Path, monkeypatch, transient_status: str
    ) -> None:
        import code_indexer.server.services.fleet_migration.scheduler as sched_mod

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "click")
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({"click": base_clone}, sqlite_backend=backend)
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        monkeypatch.setattr(
            sched_mod,
            "run_fleet_migration_for_repo",
            lambda *a, **k: FleetMigrationRepoResult(status=transient_status),
        )

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD + 2):
            result = scheduler._run_next_candidate()
            assert result["status"] == transient_status

        assert backend.get_fleet_migration_failure_state("click") is None


class TestGetStatsScopedToPendingCandidates:
    """Finding 5 (Codex, dual review round): `quarantined_repos` must only
    ever count repos that are STILL pending -- a repo could be migrated
    via a DIRECT orchestrator call outside the scheduler (reset only
    happens inside the scheduler's own success path) and still carry a
    stale quarantine row. `quarantined_repos` must never exceed
    `pending_repos`."""

    def _make_backend(self, tmp_path: Path) -> GoldenRepoMetadataSqliteBackend:
        db_path = str(tmp_path / "golden_repo_metadata.db")
        backend = GoldenRepoMetadataSqliteBackend(db_path)
        backend.ensure_table_exists()
        return backend

    def test_quarantined_repos_excludes_a_repo_migrated_outside_the_scheduler(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.storage.shared.collection_migration import (
            consolidate_collection_in_place,
        )

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        backend = self._make_backend(tmp_path)
        golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        for _ in range(FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD):
            result = scheduler._run_next_candidate()
            assert result["status"] == "dedup_gate_rejected"

        assert scheduler.get_stats()["quarantined_repos"] == 1

        # A DIRECT orchestrator/consolidation call (bypassing the
        # scheduler entirely, e.g. a manual operator remediation) fixes
        # and fully migrates the repo -- WITHOUT going through
        # scheduler._run_next_candidate()'s own reset_migration_failure()
        # success path, so the stale quarantine row is never cleared by
        # that path.
        collection_dir = (
            corrupt_base / ".code-indexer" / "index" / "semantic_collection"
        )
        (collection_dir / "cc" / "dd" / "vector_dupe0001_b.json").unlink()
        result = consolidate_collection_in_place(collection_dir)
        assert result.status == "consolidated"
        mark_post_consolidation_snapshot_published(
            corrupt_base / ".code-indexer" / "index"
        )

        stats = scheduler.get_stats()

        assert stats["migrated_repos"] == 1
        assert stats["pending_repos"] == 0
        assert stats["quarantined_repos"] == 0
        assert stats["quarantined_repos"] <= stats["pending_repos"]


class _AlwaysFailingQuarantineBackend:
    """Simulates a PERSISTENT backend read failure for is_quarantined()'s
    underlying get_fleet_migration_failure_state() call (Finding A,
    Codex round-3 review, live-reproduced)."""

    def get_fleet_migration_failure_state(self, golden_alias):
        raise RuntimeError("simulated persistent backend outage")


class TestQuarantineBackendReadFailureAbortsTheTick:
    """Finding A (HIGH, Codex round-3 review, live-reproduced): a
    PERSISTENT backend read failure must NEVER be silently treated as
    "not quarantined" -- with a persistent outage, that would make the
    scheduler proceed with the SAME first candidate on EVERY tick
    forever, recreating the EXACT fleet-starvation bug #1477 reports via
    a third path (backend outage, alongside corrupt data and a
    non-raising status). `_run_next_candidate()` must instead abort the
    scheduling tick with a distinct status -- never attempting migration,
    never returning the misleading "nothing_to_migrate"."""

    def test_persistent_backend_failure_aborts_every_tick_without_retrying_the_same_repo(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "click")
        golden = _FakeGoldenRepoManager(
            {"click": base_clone},
            sqlite_backend=_AlwaysFailingQuarantineBackend(),
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        repeated_tick_count = 3
        for _ in range(repeated_tick_count):
            result = scheduler._run_next_candidate()
            assert result["status"] == "quarantine_state_unavailable"
            assert result["golden_alias"] == "click"

        # The repo must remain untouched -- migration NEVER ran while
        # quarantine state was indeterminate, on ANY of the repeated
        # ticks above.
        collection_dir = base_clone / ".code-indexer" / "index" / "semantic_collection"
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON


class _WriteFailingQuarantineBackend:
    """Reads succeed (always reports "never failed" -- not quarantined),
    but `record_fleet_migration_failure()` genuinely fails every time
    (Finding D, Codex round-4 review, live-reproduced) -- a PERSISTENT
    backend WRITE failure, distinct from Finding A's READ failure."""

    def get_fleet_migration_failure_state(self, golden_alias):
        return None

    def record_fleet_migration_failure(
        self, golden_alias, state_signature, failure_cause=None
    ):
        raise RuntimeError("simulated persistent backend write outage")


class TestQuarantineBackendWriteFailureAbortsTheTick:
    """Finding D (HIGH, Codex round-4 review, live-reproduced): the
    round-3 fix made READ failures raise/abort, but `record_migration_
    failure()` still swallowed WRITE failures -- with reads succeeding
    (reporting "0 failures") but writes persistently failing, the
    consecutive-failure count is NEVER incremented, so the corrupt repo
    is re-selected and retried on EVERY tick forever, recreating Issue
    #1477's exact fleet-starvation bug via a write-path outage. The
    scheduler must never silently complete the tick as if bookkeeping
    succeeded -- the ORIGINAL migration exception is logged, but the net
    effect must be an explicit abort, not a silent retry loop."""

    def test_persistent_write_failure_never_silently_retries_as_if_nothing_is_wrong(
        self, tmp_path: Path
    ) -> None:
        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        golden = _FakeGoldenRepoManager(
            {"click": corrupt_base},
            sqlite_backend=_WriteFailingQuarantineBackend(),
        )
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        repeated_tick_count = 4
        for _ in range(repeated_tick_count):
            result = scheduler._run_next_candidate()
            # Never silently "succeeds" as if the corruption were
            # resolved, and never silently reports nothing_to_migrate
            # (misleading -- the repo IS pending, we just can't record
            # the failure) -- the explicit abort status is the only
            # acceptable outcome, mirroring Finding A's read-side
            # handling exactly.
            assert result["status"] == "quarantine_state_unavailable"
            assert result["golden_alias"] == "click"

        # The corrupt repo's own collection must remain untouched --
        # migration never silently "succeeded".
        collection_dir = (
            corrupt_base / ".code-indexer" / "index" / "semantic_collection"
        )
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON


class _RecoverableWriteFailingQuarantineBackend:
    """Reads always succeed (backed by a real in-memory dict); WRITES
    fail while `.writes_healthy` is False, succeed once flipped to True
    (Finding G, Codex round-5 review) -- simulates a write outage that
    later recovers, so the pre-flight health probe can be exercised
    against a REAL round-trip rather than a bare mock."""

    def __init__(self):
        self.writes_healthy = False
        self._states: dict = {}

    def get_fleet_migration_failure_state(self, golden_alias):
        return self._states.get(golden_alias)

    def record_fleet_migration_failure(
        self, golden_alias, state_signature, failure_cause=None
    ):
        if not self.writes_healthy:
            raise RuntimeError("simulated persistent backend write outage")
        row = self._states.setdefault(
            golden_alias,
            {
                "golden_alias": golden_alias,
                "consecutive_failure_count": 0,
                "state_signature": None,
                "signature_checked_at": None,
                "failure_cause": None,
            },
        )
        row["consecutive_failure_count"] += 1
        row["state_signature"] = state_signature
        row["failure_cause"] = failure_cause
        return row["consecutive_failure_count"]

    def reset_fleet_migration_failure(self, golden_alias):
        if not self.writes_healthy:
            raise RuntimeError("simulated persistent backend write outage")
        self._states.pop(golden_alias, None)

    def touch_fleet_migration_failure_check(self, golden_alias):
        row = self._states.get(golden_alias)
        if row is not None and self.writes_healthy:
            row["signature_checked_at"] = "touched"

    def list_fleet_migration_failure_states(self):
        return list(self._states.values())


class TestQuarantineWriteOutagePreflightProbe:
    """Finding G (HIGH, Codex round-5 review, live-reproduced -- the real
    blocker) + Finding J (coordinator review, round 6 -- cluster-safety
    correction): during a persistent WRITE outage, is_quarantined()'s
    READ still succeeds (reporting a stale, never-advancing count), so
    run_fleet_migration_for_repo() -- the REAL, expensive, DESTRUCTIVE
    migration call -- was previously re-invoked on EVERY tick. The fix
    is an UNCONDITIONAL cheap pre-flight health probe run before EVERY
    migration attempt on EVERY tick -- NEVER gated behind any in-process
    "did the last attempt fail" flag, since this project's absolute
    Cluster-Aware State rule forbids per-node RAM for state that must be
    visible to another HTTP request/tick in a cluster (a different node
    could pick up the very next tick with a fresh scheduler instance and
    no memory of a prior node's observation)."""

    def test_migration_never_invoked_during_persistent_write_outage_then_resumes_on_recovery(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import code_indexer.server.services.fleet_migration.scheduler as sched_mod

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        backend = _RecoverableWriteFailingQuarantineBackend()
        golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)
        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        call_count = {"n": 0}
        real_run = sched_mod.run_fleet_migration_for_repo

        def _counting_run(*args, **kwargs):
            call_count["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(sched_mod, "run_fleet_migration_for_repo", _counting_run)

        # The UNCONDITIONAL probe catches the outage on EVERY tick,
        # BEFORE the expensive migration is ever attempted -- including
        # the very FIRST tick (unlike the round-5 gated design, which
        # let the first tick through before it had "observed" a
        # failure).
        outage_tick_count = 4
        for _ in range(outage_tick_count):
            result = scheduler._run_next_candidate()
            assert result["status"] == "quarantine_state_unavailable"
        assert call_count["n"] == 0

        # Backend recovers.
        backend.writes_healthy = True

        # Next tick: the probe succeeds -- migration resumes normally
        # (attempted for the first time, fails for the SAME underlying
        # corruption, and bookkeeping succeeds this time).
        result = scheduler._run_next_candidate()
        assert result["status"] == "dedup_gate_rejected"
        assert call_count["n"] == 1

        recorded_state = backend.get_fleet_migration_failure_state("click")
        assert recorded_state is not None
        assert recorded_state["consecutive_failure_count"] == 1

    def test_no_dependency_on_scheduler_instance_identity_across_two_fresh_instances(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Simulates 'the outage is observed on node A's tick, the very
        next tick runs on node B' -- TWO SEPARATE, FRESH
        FleetMigrationScheduler instances sharing ONLY the real
        persisted backend, with ZERO in-process state carried between
        them. A correct, cluster-safe implementation behaves identically
        regardless of which instance runs which tick."""
        import code_indexer.server.services.fleet_migration.scheduler as sched_mod

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        corrupt_base = _build_corrupt_repo_with_duplicate_point_id(
            golden_repos_dir, "click"
        )
        backend = _RecoverableWriteFailingQuarantineBackend()
        golden = _FakeGoldenRepoManager({"click": corrupt_base}, sqlite_backend=backend)

        call_count = {"n": 0}
        real_run = sched_mod.run_fleet_migration_for_repo

        def _counting_run(*args, **kwargs):
            call_count["n"] += 1
            return real_run(*args, **kwargs)

        monkeypatch.setattr(sched_mod, "run_fleet_migration_for_repo", _counting_run)

        def _make_fresh_scheduler():
            return _make_scheduler(
                tmp_path,
                golden,
                refresh_scheduler,
                background_job_manager=MagicMock(),
                config_service=_RecordingConfigService(enabled=True),
            )

        # "Node A": a brand-new scheduler instance observes the ongoing
        # outage and correctly aborts -- with NO prior in-process state
        # at all (this is its first-ever call).
        scheduler_node_a = _make_fresh_scheduler()
        result_a = scheduler_node_a._run_next_candidate()
        assert result_a["status"] == "quarantine_state_unavailable"
        assert call_count["n"] == 0

        # "Node B": a SEPARATE, ALSO brand-new scheduler instance -- it
        # has NO memory whatsoever of node A's observation (a real
        # in-process dict-based flag would have been empty here too,
        # but the point is this design carries NO such flag at all). The
        # outage is STILL ongoing, so node B must independently detect
        # it via its own unconditional probe call, not rely on anything
        # node A supposedly "remembered".
        scheduler_node_b = _make_fresh_scheduler()
        result_b = scheduler_node_b._run_next_candidate()
        assert result_b["status"] == "quarantine_state_unavailable"
        assert call_count["n"] == 0

        # Backend recovers (shared by both instances, since it is the
        # REAL persisted backend, never per-instance state).
        backend.writes_healthy = True

        # A THIRD fresh instance ("node C", or node A/B running again)
        # succeeds the probe and resumes migration -- proving recovery
        # is visible through the SHARED backend alone, never through any
        # scheduler-instance's own memory.
        scheduler_node_c = _make_fresh_scheduler()
        result = scheduler_node_c._run_next_candidate()
        assert result["status"] == "dedup_gate_rejected"
        assert call_count["n"] == 1


class _ResetFailingQuarantineBackend:
    """Reads/writes succeed normally (backed by a real in-memory dict),
    but `reset_fleet_migration_failure()` always fails (Finding H, Codex
    round-5 review, live-reproduced) -- simulates UPDATE working while
    DELETE is specifically broken."""

    def __init__(self):
        self._states: dict = {}

    def get_fleet_migration_failure_state(self, golden_alias):
        return self._states.get(golden_alias)

    def record_fleet_migration_failure(
        self, golden_alias, state_signature, failure_cause=None
    ):
        row = self._states.setdefault(
            golden_alias,
            {
                "golden_alias": golden_alias,
                "consecutive_failure_count": 0,
                "state_signature": None,
                "signature_checked_at": None,
                "failure_cause": None,
            },
        )
        row["consecutive_failure_count"] += 1
        row["state_signature"] = state_signature
        row["failure_cause"] = failure_cause
        return row["consecutive_failure_count"]

    def reset_fleet_migration_failure(self, golden_alias) -> None:
        raise RuntimeError("simulated persistent backend DELETE outage")

    def touch_fleet_migration_failure_check(self, golden_alias) -> None:
        row = self._states.get(golden_alias)
        if row is not None:
            row["signature_checked_at"] = "touched"

    def list_fleet_migration_failure_states(self):
        return list(self._states.values())


class TestSchedulerResetFailureOnSuccessDoesNotAffectMigrationResult:
    """Finding H (MEDIUM, Codex round-5 review): a completed migration
    whose quarantine-cleanup (reset) failed must still report the
    migration's OWN success (don't punish the user for the migration
    itself) -- but must not silently claim quarantine state was cleared
    when it wasn't; the stale row must remain genuinely visible on a
    subsequent read."""

    def test_reset_failure_on_completed_status_does_not_affect_the_reported_result(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.fleet_migration.quarantine import (
            record_migration_failure,
        )

        refresh_scheduler = _make_refresh_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        base_clone = _build_unconsolidated_repo(golden_repos_dir, "click")
        backend = _ResetFailingQuarantineBackend()
        golden = _FakeGoldenRepoManager({"click": base_clone}, sqlite_backend=backend)
        # Pre-seed a prior failure record, as if earlier attempts had
        # failed -- there is now something for the "completed" status's
        # reset call to (attempt to) clear.
        record_migration_failure(golden, "click", "stale-signature")

        scheduler = _make_scheduler(
            tmp_path,
            golden,
            refresh_scheduler,
            background_job_manager=MagicMock(),
            config_service=_RecordingConfigService(enabled=True),
        )

        result = scheduler._run_next_candidate()

        assert result["status"] == "completed"
        # Since reset (DELETE) is broken, the stale failure row must
        # still be visible -- never silently claimed as cleared when it
        # was not.
        assert backend.get_fleet_migration_failure_state("click") is not None
