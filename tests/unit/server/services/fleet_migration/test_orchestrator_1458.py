"""Unit/integration tests for run_fleet_migration_for_repo() (Story #1458
AC1/AC1a/AC2/AC8/AC9/AC10 -- the per-repo fleet-migration orchestration
sequence).

Real WriteLockManager (via a real RefreshScheduler), real JobTracker backed
by a real SQLite background_jobs table, real ChunkStore/SQLite consolidation,
real AliasManager/VersionedSnapshotManager publication -- no mocking of the
storage layer under test.
"""

import json
import sqlite3
from pathlib import Path

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.fleet_migration.completion_gate import (
    repo_has_published_post_consolidation_snapshot,
)
from code_indexer.server.services.fleet_migration.orchestrator import (
    MIGRATION_OWNER_NAME,
    TemporalNamespaceSpec,
    run_fleet_migration_for_repo,
)
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


_BACKGROUND_JOBS_DDL = """
    CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY,
        operation_type TEXT,
        status TEXT,
        created_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER DEFAULT 0,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        cancelled INTEGER DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER DEFAULT 0,
        progress_info TEXT,
        metadata TEXT,
        actor_username TEXT
    )
"""


def _make_job_tracker(tmp_path: Path) -> JobTracker:
    db_path = str(tmp_path / "tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_BACKGROUND_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()
    return JobTracker(db_path)


def _make_scheduler(tmp_path: Path, job_tracker=None) -> RefreshScheduler:
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
        job_tracker=job_tracker,
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


def _setup_base_clone(tmp_path: Path) -> Path:
    base_clone = tmp_path / "base-clone"
    index_path = base_clone / ".code-indexer" / "index"
    index_path.mkdir(parents=True)
    (base_clone / ".code-indexer" / "config.json").write_text("{}")
    return base_clone


class TestRunFleetMigrationForRepoHappyPath:
    def test_full_pass_consolidates_and_publishes_snapshot(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"

        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir()
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": 2})
        )
        _write_vector_json(collection_dir, "aaaa1111", [0.1, 0.2])

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

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
        )

        assert result.status == "completed"
        assert result.collections_consolidated == 1
        assert result.snapshot_path is not None
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB
        assert (
            scheduler.alias_manager.read_alias("evolution-global")
            == result.snapshot_path
        )
        assert repo_has_published_post_consolidation_snapshot(index_path) is True

    def test_write_lock_released_after_successful_run(self, tmp_path: Path) -> None:
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert scheduler.is_write_locked("evolution") is False


class TestRunFleetMigrationForRepoAC1Ordering:
    def test_temporal_namespaces_bootstrapped_before_snapshot_fires(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"

        legacy_shard_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        _write_vector_json(legacy_shard_dir, "row00001", [0.1, 0.2])

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[
                TemporalNamespaceSpec(
                    pointer_namespace="evolution-temporal-voyage_code_3-2024Q1",
                    legacy_shard_dir=legacy_shard_dir,
                    embedder_slug="voyage_code_3",
                )
            ],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert result.status == "completed"
        assert result.temporal_namespaces_processed == 1
        assert not legacy_shard_dir.exists()
        assert sister_alias_manager.alias_exists(
            "evolution-temporal-voyage_code_3-2024Q1"
        )
        assert result.snapshot_path is not None

    def test_incomplete_when_residual_temporal_dir_remains(
        self, tmp_path: Path
    ) -> None:
        # Simulate a repo with a residual temporal dir that was NOT passed
        # in temporal_namespaces (e.g. discovery gap upstream) -- the
        # completion gate must still catch it and refuse to fire AC10.
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        (index_path / "code-indexer-temporal-voyage_code_3-2024Q1").mkdir()

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert result.status == "incomplete"
        assert result.snapshot_path is None
        assert scheduler.alias_manager.read_alias("evolution-global") is None

    def test_marker_absent_when_status_is_incomplete(self, tmp_path: Path) -> None:
        """New CRITICAL finding: the durable snapshot-published marker
        must NEVER be written when the snapshot did not actually fire."""
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        (index_path / "code-indexer-temporal-voyage_code_3-2024Q1").mkdir()

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert result.status == "incomplete"
        assert repo_has_published_post_consolidation_snapshot(index_path) is False


class TestRunFleetMigrationForRepoAC1aRowlessEmptyArtifact:
    def test_repo_whose_only_residual_temporal_dir_is_rowless_empty_artifact_completes(
        self, tmp_path: Path
    ) -> None:
        # AC1a (Finding 6): a rowless in-repo temporal directory (no
        # committed rows, no sister pointer) must be swept as an EMPTY
        # ARTIFACT in the SAME migration pass, driving the repo to
        # COMPLETE -- never permanently blocking the completion gate.
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"

        rowless_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        rowless_dir.mkdir(parents=True)
        # Deliberately zero vector_*.json rows -- a failed/empty prior
        # indexing attempt that left directory structure behind.

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[
                TemporalNamespaceSpec(
                    pointer_namespace="evolution-temporal-voyage_code_3-2024Q1",
                    legacy_shard_dir=rowless_dir,
                    embedder_slug="voyage_code_3",
                )
            ],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert result.status == "completed"
        assert not rowless_dir.exists()
        # Never published to the sister location -- nothing to migrate.
        assert not sister_alias_manager.alias_exists(
            "evolution-temporal-voyage_code_3-2024Q1"
        )
        assert result.snapshot_path is not None


class TestRunFleetMigrationForRepoRefusesImmutablePath:
    """Defense-in-depth (Codex round-2 follow-up): discovery.py already
    skips a candidate whose base_clone_path resolves to an immutable
    .versioned/ snapshot, but the destructive engine itself must ALSO
    refuse -- never trust the caller alone, per this project's absolute
    'NEVER modify/checkout/index inside .versioned/' invariant."""

    def test_refuses_to_touch_a_base_clone_path_that_resolves_to_an_immutable_versioned_snapshot(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_scheduler(tmp_path)
        golden_repos_dir = tmp_path / "golden-repos"
        immutable_snapshot_path = (
            golden_repos_dir / ".versioned" / "evolution" / "v_1700000000"
        )
        index_path = immutable_snapshot_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=immutable_snapshot_path,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert result.status == "refused_immutable_path"
        assert scheduler.is_write_locked("evolution") is False


class TestConsolidateSemanticCollectionsRefusesSymlinkIntoImmutableSnapshot:
    """Codex round-6 CRITICAL finding #4 (TOCTOU, only partially fixed by
    discovery.py's static symlink check): discovery returns plain
    pathnames, and this destructive consolidation helper re-resolves
    them LATER -- a symlink swapped in AFTER discovery but BEFORE
    consolidation still gets followed. Real repro (Codex):
    swap a real directory for a symlink into .versioned/ between
    discovery and consolidation -- the immutable snapshot gets a
    chunks.db written into it, its discriminator flips, and its
    original source shard is DELETED. Fix: re-validate symlink-status +
    .versioned exclusion immediately before the destructive write."""

    def test_refuses_a_collection_dir_that_is_a_symlink_at_call_time(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.services.fleet_migration.orchestrator import (
            _consolidate_semantic_collections,
        )
        from code_indexer.server.services.query_path_cache import (
            is_immutable_versioned_snapshot,
        )

        golden_repos_dir = tmp_path / "golden-repos"
        immutable_collection = (
            golden_repos_dir
            / ".versioned"
            / "repo-a"
            / "v_1700000000"
            / ".code-indexer"
            / "index"
            / "semantic"
        )
        immutable_collection.mkdir(parents=True)
        (immutable_collection / "collection_meta.json").write_text(
            json.dumps({"name": "semantic", "vector_size": 4})
        )
        shard = immutable_collection / "sn" / "01"
        shard.mkdir(parents=True)
        immutable_source = shard / "vector_snap0001.json"
        immutable_source.write_text(
            json.dumps(
                {
                    "id": "snap0001",
                    "vector": [1.0, 2.0, 3.0, 4.0],
                    "metadata": {},
                    "payload": {"path": "src/x.py"},
                    "chunk_text": "x",
                }
            )
        )

        repo = golden_repos_dir / "repo-a"
        symlinked_collection = repo / ".code-indexer" / "index" / "semantic"
        symlinked_collection.parent.mkdir(parents=True)
        symlinked_collection.symlink_to(immutable_collection, target_is_directory=True)

        # Test-setup invariant: prove the symlink genuinely resolves into
        # the immutable tree, so this test exercises the real hazard.
        assert (
            is_immutable_versioned_snapshot(str(symlinked_collection.resolve())) is True
        )

        try:
            consolidated_count, _skipped = _consolidate_semantic_collections(
                [symlinked_collection]
            )
            raised = None
        except Exception as exc:  # noqa: BLE001 -- proving SOME refusal fires
            consolidated_count = 0
            raised = exc

        assert raised is not None or consolidated_count == 0, (
            "Bug: a symlinked collection directory pointing into the "
            "immutable .versioned/ tree was silently consolidated "
            "instead of being refused."
        )
        assert not (immutable_collection / "chunks.db").exists(), (
            "Bug: chunks.db was written INTO the immutable snapshot "
            "through the symlink."
        )
        assert immutable_source.exists(), (
            "Bug: the immutable snapshot's original legacy source file "
            "was deleted by cleanup after consolidating through the "
            "symlink."
        )


class TestSnapshotGateReVerifiesBeforeFiring:
    """Codex round-6 HIGH finding #7: the snapshot gate only checked
    disk-skip flags and temporal-absence -- it never called
    verify_collection_fully_migrated() fresh, immediately before
    publishing. The duplicate-ID bug (finding #5) can return
    status="consolidated" despite actually having lost/residual data,
    and the (pre-fix) snapshot gate would publish it anyway. Fix: call
    the full verification function on every semantic collection
    immediately before the snapshot fires."""

    def test_snapshot_does_not_fire_when_a_collection_status_lies_about_being_consolidated(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"

        # A collection dir that is genuinely NOT migrated (no chunks.db,
        # no discriminator) -- verify_collection_fully_migrated() must
        # return False for it, regardless of what a caller claims.
        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir()
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": 2})
        )

        import code_indexer.server.services.fleet_migration.orchestrator as orch_module
        from code_indexer.storage.shared.collection_migration import (
            ConsolidationResult,
        )

        def _lying_consolidate(collection_dir_arg, **kwargs):
            # Simulates finding #5's duplicate-ID bug: claims success
            # without the collection actually being verifiably migrated.
            # **kwargs absorbs Story #1460's deletion_authorized param --
            # irrelevant to this test's own concern (a lying status).
            return ConsolidationResult(status="consolidated")

        monkeypatch.setattr(
            orch_module, "consolidate_collection_in_place", _lying_consolidate
        )

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

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
        )

        assert result.status != "completed", (
            "Bug: the AC10 snapshot fired despite the collection never "
            "being genuinely, verifiably migrated -- the gate trusted "
            "consolidate_collection_in_place()'s returned status instead "
            "of re-verifying fresh immediately before publishing."
        )
        assert result.snapshot_path is None
        assert repo_has_published_post_consolidation_snapshot(index_path) is False


class TestRunFleetMigrationForRepoAC9RefreshInFlight:
    def test_refuses_to_start_when_refresh_is_active(self, tmp_path: Path) -> None:
        job_tracker = _make_job_tracker(tmp_path)
        scheduler = _make_scheduler(tmp_path, job_tracker=job_tracker)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir()
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": 2})
        )
        _write_vector_json(collection_dir, "bbbb2222", [0.3, 0.4])

        job_tracker.register_job(
            "refresh-evolution-global",
            operation_type="global_repo_refresh",
            username="system",
            repo_alias="evolution-global",
        )
        job_tracker.update_status("refresh-evolution-global", status="running")

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

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
        )

        assert result.status == "refresh_in_flight"
        # Migration must NOT have touched the collection at all.
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON

    def test_write_lock_released_when_refresh_in_flight(self, tmp_path: Path) -> None:
        job_tracker = _make_job_tracker(tmp_path)
        scheduler = _make_scheduler(tmp_path, job_tracker=job_tracker)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"

        job_tracker.register_job(
            "refresh-evolution-global",
            operation_type="global_repo_refresh",
            username="system",
            repo_alias="evolution-global",
        )
        job_tracker.update_status("refresh-evolution-global", status="running")

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        # Not left holding the lock forever -- released so a retry (or
        # refresh, or activation) can proceed.
        assert scheduler.is_write_locked("evolution") is False


class TestRunFleetMigrationForRepoAC2AC8WriteLock:
    def test_second_concurrent_migration_call_is_refused_lock_held(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"

        # Simulate an in-flight migration by directly holding the lock
        # under the SAME owner name migration itself uses.
        acquired = scheduler.write_lock_manager.acquire(
            "evolution", owner_name=MIGRATION_OWNER_NAME, ttl_seconds=3600
        )
        assert acquired is True

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[],
            temporal_namespaces=[],
            sister_root=sister_root,
            deletion_authorized=True,
        )

        assert result.status == "lock_held"

    def test_lock_does_not_go_stale_beyond_default_ttl_while_migration_runs(
        self, tmp_path: Path
    ) -> None:
        # AC8: migration acquires a long, explicitly-justified TTL (NOT the
        # 3600s default) so a legitimately long-running migration is never
        # evicted as stale mid-run. Simulate elapsed time well beyond the
        # base 3600s default and assert a concurrent acquire from another
        # owner is STILL refused (the lock has not gone stale).
        import json as _json
        from datetime import datetime, timedelta, timezone

        scheduler = _make_scheduler(tmp_path)
        acquired = scheduler.write_lock_manager.acquire(
            "evolution", owner_name=MIGRATION_OWNER_NAME, ttl_seconds=24 * 60 * 60
        )
        assert acquired is True

        lock_file = scheduler.golden_repos_dir / ".locks" / "evolution.lock"
        content = _json.loads(lock_file.read_text())
        # Backdate acquired_at by 2 hours -- well beyond the base 3600s
        # default TTL, but still far inside the migration-specific TTL.
        content["acquired_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        lock_file.write_text(_json.dumps(content))

        still_refused = scheduler.write_lock_manager.acquire(
            "evolution", owner_name="some-other-writer"
        )
        assert still_refused is False


class TestRunFleetMigrationForRepoAC7ActivationFailFast:
    def test_activation_style_acquire_fails_fast_while_migration_holds_lock(
        self, tmp_path: Path
    ) -> None:
        # AC7: activation attempts the SAME non-blocking WriteLockManager
        # acquire() ActivatedRepoManager already uses -- proving migration
        # holding the lock produces the identical fail-fast (returns False
        # immediately, never blocks/hangs) behavior activation already
        # relies on for refresh-vs-activation conflicts (Bug #1393).
        scheduler = _make_scheduler(tmp_path)
        acquired = scheduler.write_lock_manager.acquire(
            "evolution", owner_name=MIGRATION_OWNER_NAME, ttl_seconds=3600
        )
        assert acquired is True

        import time

        start = time.monotonic()
        activation_acquired = scheduler.write_lock_manager.acquire(
            "evolution", owner_name="activation"
        )
        elapsed = time.monotonic() - start

        assert activation_acquired is False
        assert elapsed < 1.0  # non-blocking -- returns immediately
