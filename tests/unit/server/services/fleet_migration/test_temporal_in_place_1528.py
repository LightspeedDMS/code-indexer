"""Bug #1528 item 3: fleet migration must consolidate legacy TEMPORAL
shards IN PLACE -- through the SAME ``consolidate_collection_in_place``
engine it already uses for semantic collections -- instead of publishing a
duplicate copy to the Story #1457 "sister location".

The sister-location bootstrap was additive and location-changing: it built a
second consolidated copy under ``golden-repos/.versioned/...``, published an
alias pointer to it, and only then reclaimed the in-repo tree. That is a
parallel migration system for temporal alone. With temporal now writing
CHUNKS_DB natively (this bug's items 1-2), the ONE in-place engine covers
both collection kinds, so migration keeps a temporal shard exactly where it
is and merely changes its internal layout.

Real RefreshScheduler/WriteLockManager, real FilesystemVectorStore-written
legacy fixture, real SQLite consolidation -- no mocking of the code under
test.
"""

from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

from code_indexer.config import ConfigManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.fleet_migration.orchestrator import (
    FleetMigrationRepoResult,
    TemporalNamespaceSpec,
    run_fleet_migration_for_repo,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout

EMBEDDER_SLUG = "voyage_code_3"
SHARD_NAME = f"code-indexer-temporal-{EMBEDDER_SLUG}-2024Q1"
POINTER_NAMESPACE = f"evolution-temporal-{EMBEDDER_SLUG}-2024Q1"
REPO_ALIAS = "evolution"

VECTOR_SIZE = 16
RNG_SEED = 1528
#: Git object ids are 40 hex characters; the fixture zero-pads a row index
#: to that width so payloads look like real commit hashes.
COMMIT_HASH_HEX_WIDTH = 40
#: Temporal point ids are "{project}:commit:{hash}:{chunk_index}"; the
#: fixture uses a single chunk per commit.
CHUNK_INDEX = 0
#: Distinct, deterministic stand-in commit hashes for the fixture rows.
COMMIT_STUB_WIDTH = 8
ROW_IDS = [f"proj:commit:{char * COMMIT_STUB_WIDTH}:{CHUNK_INDEX}" for char in "ab"]


def _make_scheduler(tmp_path: Path) -> RefreshScheduler:
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    versioned_base = tmp_path / "versioned"
    versioned_base.mkdir(parents=True, exist_ok=True)

    query_tracker = QueryTracker()
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
        snapshot_manager=VersionedSnapshotManager(versioned_base=str(versioned_base)),
    )


def _legacy_points() -> List[Dict[str, Any]]:
    rng = np.random.default_rng(RNG_SEED)
    return [
        {
            "id": pid,
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {
                "path": f"src/a{i}.py",
                "commit_hash": f"{i:0{COMMIT_HASH_HEX_WIDTH}d}",
            },
            "chunk_text": f"chunk {i}",
        }
        for i, pid in enumerate(ROW_IDS)
    ]


def _write_legacy_temporal_shard(index_path: Path) -> Path:
    """Build a REAL legacy (SHARDED_JSON) temporal shard with the production
    writer, by explicitly requesting the legacy layout."""
    store = FilesystemVectorStore(
        base_path=index_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    store.begin_indexing(SHARD_NAME)
    store.upsert_points(SHARD_NAME, _legacy_points())
    store.end_indexing(SHARD_NAME)

    shard_dir = index_path / SHARD_NAME
    assert list(shard_dir.rglob("vector_*.json")), "fixture is not legacy-layout"
    return shard_dir


def _setup_base_clone(tmp_path: Path) -> Path:
    base_clone = tmp_path / "base-clone"
    index_path = base_clone / ".code-indexer" / "index"
    index_path.mkdir(parents=True)
    (base_clone / ".code-indexer" / "config.json").write_text("{}")
    return base_clone


def _namespace_spec(shard_dir: Path) -> TemporalNamespaceSpec:
    return TemporalNamespaceSpec(
        pointer_namespace=POINTER_NAMESPACE,
        legacy_shard_dir=shard_dir,
        embedder_slug=EMBEDDER_SLUG,
    )


def _run(
    tmp_path: Path, index_path: Path, namespaces: List[TemporalNamespaceSpec]
) -> Tuple[FleetMigrationRepoResult, AliasManager]:
    sister_root = tmp_path / "sister"
    sister_alias_manager = AliasManager(str(sister_root / "aliases"))
    result = run_fleet_migration_for_repo(
        refresh_scheduler=_make_scheduler(tmp_path),
        sister_alias_manager=sister_alias_manager,
        repo_alias=REPO_ALIAS,
        base_clone_path=index_path.parent.parent,
        index_path=index_path,
        semantic_collection_dirs=[],
        temporal_namespaces=namespaces,
        sister_root=sister_root,
        deletion_authorized=True,
    )
    return result, sister_alias_manager


class TestTemporalConsolidatedInPlace:
    def test_shard_stays_in_place_and_becomes_chunks_db(self, tmp_path: Path) -> None:
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        shard_dir = _write_legacy_temporal_shard(index_path)

        result, sister_alias_manager = _run(
            tmp_path, index_path, [_namespace_spec(shard_dir)]
        )

        assert result.status == "completed", result.detail
        assert result.temporal_namespaces_processed == 1
        assert shard_dir.is_dir(), "temporal shard must be migrated IN PLACE"
        assert (shard_dir / "chunks.db").is_file()
        assert list(shard_dir.rglob("vector_*.json")) == []
        assert resolve_chunk_layout(shard_dir) == ChunkLayout.CHUNKS_DB
        assert not sister_alias_manager.alias_exists(POINTER_NAMESPACE), (
            "in-place migration must not publish a duplicate sister copy"
        )
        assert result.snapshot_path is not None

    def test_rows_survive_the_in_place_migration(self, tmp_path: Path) -> None:
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        shard_dir = _write_legacy_temporal_shard(index_path)

        result, _ = _run(tmp_path, index_path, [_namespace_spec(shard_dir)])
        assert result.status == "completed", result.detail

        reader = FilesystemVectorStore(base_path=index_path)
        for pid in ROW_IDS:
            assert reader.get_point(pid, SHARD_NAME) is not None, f"lost row {pid}"


class TestCompletionGateForInPlaceTemporal:
    def test_still_legacy_temporal_dir_keeps_gate_closed(self, tmp_path: Path) -> None:
        """A temporal shard left in the LEGACY layout (e.g. missed by
        discovery) must keep the completion gate closed -- physical presence
        of the directory is no longer the signal, its layout is."""
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        _write_legacy_temporal_shard(index_path)

        result, _ = _run(tmp_path, index_path, [])

        assert result.status == "incomplete"
        assert result.snapshot_path is None

    def test_consolidated_temporal_dir_passes_gate(self, tmp_path: Path) -> None:
        """A temporal shard already fully consolidated in place passes the
        gate even though its directory is still physically present."""
        base_clone = _setup_base_clone(tmp_path)
        index_path = base_clone / ".code-indexer" / "index"
        shard_dir = _write_legacy_temporal_shard(index_path)

        first, _ = _run(tmp_path, index_path, [_namespace_spec(shard_dir)])
        assert first.status == "completed", first.detail

        second, _ = _run(tmp_path, index_path, [])
        assert second.status == "completed", second.detail
