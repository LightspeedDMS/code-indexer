"""Unit/integration tests for run_fleet_migration_for_repo()'s rollout
-safety deletion gate (Story #1460 AC1/AC2, Epic #1454).

Story #1458's orchestrator called consolidate_collection_in_place() and
bootstrap_temporal_namespace_to_sister() unconditionally -- the ONLY thing
standing between a direct call and real on-disk deletion was whatever the
CALLER happened to check beforehand (in production, only
FleetMigrationScheduler's own `_is_enabled_now()` check). Story #1460 closes
that gap with genuine defense-in-depth: `run_fleet_migration_for_repo()` now
accepts an explicit `deletion_authorized: Optional[bool]` override and, when
not given (None), resolves it itself from the SAME operator-controlled,
`get_config_service()`-backed `fleet_migration_config.enabled` flag Story
#1458 already wired through the Web UI Config Screen -- so ANY caller
(scheduler, a hypothetical future admin trigger, or a test) that invokes
this function without explicitly overriding the gate gets the real,
default-OFF, fail-closed config value, never silent unconditional deletion.

Real WriteLockManager (via a real RefreshScheduler), real ChunkStore/SQLite
consolidation, real AliasManager publication -- no mocking of the storage
layer under test. The config surface itself uses the SAME lightweight fake
(`_RecordingConfigService`) test_scheduler_1458.py already established for
exercising `fleet_migration_config.enabled`, injected via the real
`set_config_service`/`reset_config_service` singleton hooks.
"""

import json
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.config_service import (
    reset_config_service,
    set_config_service,
)
from code_indexer.server.services.fleet_migration.discovery import (
    FleetMigrationCandidate,
    is_repo_already_migrated,
)
from code_indexer.server.services.fleet_migration.orchestrator import (
    TemporalNamespaceSpec,
    run_fleet_migration_for_repo,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout


class _RecordingConfigService:
    """Same minimal fake test_scheduler_1458.py already established for
    exercising fleet_migration_config.enabled -- a test double, not a mock
    of the code under test."""

    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self.tick_interval_minutes = 30

    def get_config(self):
        cfg = self

        class _Wrapper:
            fleet_migration_config = cfg

        return _Wrapper()


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


def _vector_json_path(collection_dir: Path, point_id: str) -> Path:
    return collection_dir / point_id[:2] / point_id[2:4] / f"vector_{point_id}.json"


def _write_vector_json(collection_dir: Path, point_id: str, vector) -> None:
    file_path = _vector_json_path(collection_dir, point_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {},
        "payload": {"path": "src/a.py"},
        "chunk_text": "x",
    }
    file_path.write_text(json.dumps(record))


def _setup_base_clone(tmp_path: Path) -> Path:
    base_clone = tmp_path / "base-clone"
    index_path = base_clone / ".code-indexer" / "index"
    index_path.mkdir(parents=True)
    (base_clone / ".code-indexer" / "config.json").write_text("{}")
    return base_clone


@pytest.fixture(autouse=True)
def _reset_config_service_singleton():
    reset_config_service()
    yield
    reset_config_service()


class TestExplicitDeletionAuthorizedOverrideBypassesConfig:
    def test_explicit_true_deletes_regardless_of_config(self, tmp_path: Path) -> None:
        # No config service registered at all -- proves the explicit
        # override short-circuits config resolution entirely.
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
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB

    def test_explicit_false_withholds_deletion_and_never_completes(
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
        _write_vector_json(collection_dir, "bbbb2222", [0.3, 0.4])
        vfile = _vector_json_path(collection_dir, "bbbb2222")

        legacy_shard_dir = index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
        legacy_shard_dir.mkdir(parents=True, exist_ok=True)
        (legacy_shard_dir / "collection_meta.json").write_text(
            json.dumps(
                {
                    "name": "code-indexer-temporal-voyage_code_3-2024Q1",
                    "vector_size": 2,
                }
            )
        )
        _write_vector_json(legacy_shard_dir, "row00001", [0.1, 0.2])

        sister_root = tmp_path / "sister"
        sister_alias_manager = AliasManager(str(sister_root / "aliases"))

        result = run_fleet_migration_for_repo(
            refresh_scheduler=scheduler,
            sister_alias_manager=sister_alias_manager,
            repo_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[collection_dir],
            temporal_namespaces=[
                TemporalNamespaceSpec(
                    pointer_namespace="evolution-temporal-voyage_code_3-2024Q1",
                    legacy_shard_dir=legacy_shard_dir,
                    embedder_slug="voyage_code_3",
                )
            ],
            sister_root=sister_root,
            deletion_authorized=False,
        )

        # Both semantic AND temporal deletion withheld -- the completion
        # gate correctly refuses to report this repo done, so the AC10
        # snapshot never fires while the rollout gate is closed.
        assert result.status == "incomplete"
        assert result.snapshot_path is None
        assert vfile.exists()
        assert legacy_shard_dir.exists()
        # Bug #1528 bake window, in place: the shard's consolidated
        # chunks.db is already written and committed in the SAME directory
        # while its legacy vector_*.json rows are deliberately left behind
        # (deletion withheld) -- both an old and a new reader see correct
        # data. No duplicate sister copy is published any more.
        assert (legacy_shard_dir / "chunks.db").is_file()
        assert list(legacy_shard_dir.rglob("vector_*.json"))
        assert not sister_alias_manager.alias_exists(
            "evolution-temporal-voyage_code_3-2024Q1"
        )

        candidate = FleetMigrationCandidate(
            sort_key="evolution",
            golden_alias="evolution",
            base_clone_path=base_clone,
            index_path=index_path,
            semantic_collection_dirs=[collection_dir],
            temporal_namespaces=[],
            sister_root=sister_root,
            sister_alias_manager=sister_alias_manager,
        )
        assert is_repo_already_migrated(candidate) is False


class TestDeletionAuthorizedResolvesFromConfigWhenNotGiven:
    def test_resolves_false_from_config_when_fleet_migration_disabled(
        self, tmp_path: Path
    ) -> None:
        set_config_service(_RecordingConfigService(enabled=False))

        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir()
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": 2})
        )
        _write_vector_json(collection_dir, "cccc3333", [0.5, 0.6])
        vfile = _vector_json_path(collection_dir, "cccc3333")

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
        )

        assert result.status == "incomplete"
        assert vfile.exists()

    def test_resolves_true_from_config_when_fleet_migration_enabled(
        self, tmp_path: Path
    ) -> None:
        set_config_service(_RecordingConfigService(enabled=True))

        scheduler = _make_scheduler(tmp_path)
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        collection_dir = index_path / "semantic_collection"
        collection_dir.mkdir()
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "vector_size": 2})
        )
        _write_vector_json(collection_dir, "dddd4444", [0.7, 0.8])
        vfile = _vector_json_path(collection_dir, "dddd4444")

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
        )

        assert result.status == "completed"
        assert not vfile.exists()
