"""GitHub Bug #1775 remediation: `trigger_post_consolidation_snapshot()`
(fleet migration's final, exactly-once-per-repo, post-consolidation
publish step) never invalidated EITHER cache before this fix -- a fifth
real production alias-swap site, distinct from `_cb_swap_alias()`,
`RefreshScheduler._execute_refresh_impl()`,
`GoldenRepoManager.add_indexes_to_golden_repo()`'s add-index publish, and
`_post_provider_index_snapshot()`.

Reuses `test_snapshot_trigger_1458.py`'s proven, fully-real harness
verbatim (real `RefreshScheduler`, real `VersionedSnapshotManager` local
CoW mode, real `AliasManager`, real `QueryTracker`/`CleanupManager` -- no
mocking of the storage layer under test). Calls the function twice: the
first publish creates a genuine versioned snapshot with real copied
content (a real chunks.db), which becomes the "old" target once the
second publish swaps the alias to a new snapshot.
"""

from pathlib import Path

import hnswlib

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.cache import get_global_cache, reset_global_cache
from code_indexer.server.services.fleet_migration.snapshot_trigger import (
    trigger_post_consolidation_snapshot,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.storage.shared.chunk_store_cache import (
    get_global_chunk_store_cache,
    reset_global_chunk_store_cache,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
INDEX_SUBPATH = Path(".code-indexer") / "index" / PROVIDER_DIR
HNSW_SPACE = "cosine"
HNSW_DIM = 4
HNSW_MAX_ELEMENTS = 10
HNSW_EF_CONSTRUCTION = 10
HNSW_M = 4


def _make_real_hnsw_index() -> hnswlib.Index:
    idx = hnswlib.Index(space=HNSW_SPACE, dim=HNSW_DIM)
    idx.init_index(
        max_elements=HNSW_MAX_ELEMENTS, ef_construction=HNSW_EF_CONSTRUCTION, M=HNSW_M
    )
    return idx


def _make_real_scheduler(tmp_path: Path) -> RefreshScheduler:
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
    )


def _seed_source_with_real_chunk_store(source_path: Path) -> None:
    collection_dir = source_path / INDEX_SUBPATH
    collection_dir.mkdir(parents=True, exist_ok=True)
    store = ChunkStore(collection_dir / CHUNKS_DB_FILENAME)
    try:
        store.write_batch(
            [{"id": "p1", "vector": VECTOR, "payload": {"path": "p1.py"}}]
        )
    finally:
        store.close()


class TestTriggerPostConsolidationSnapshotInvalidatesBothCaches1775:
    def setup_method(self) -> None:
        reset_global_cache()
        reset_global_chunk_store_cache()

    def teardown_method(self) -> None:
        get_global_chunk_store_cache().close_current_thread()
        reset_global_cache()
        reset_global_chunk_store_cache()

    def test_second_publish_invalidates_hnsw_and_chunk_store_caches_for_old_snapshot(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_real_scheduler(tmp_path)
        source_path = tmp_path / "base-clone"
        source_path.mkdir(parents=True)
        _seed_source_with_real_chunk_store(source_path)

        first_target = trigger_post_consolidation_snapshot(
            scheduler, "evolution", str(source_path)
        )
        old_coll = str(Path(first_target) / INDEX_SUBPATH)
        old_db = str(Path(old_coll) / CHUNKS_DB_FILENAME)
        assert Path(old_db).exists()

        chunk_cache = get_global_chunk_store_cache()
        store_first = chunk_cache.get_or_open(old_db, old_coll)
        try:
            assert store_first.read("p1") is not None

            hnsw_cache = get_global_cache()
            hnsw_cache.get_or_load(old_coll, lambda: (_make_real_hnsw_index(), {}))

            # Mutate the base clone and re-publish -- same recipe as
            # test_snapshot_trigger_1458.py's republish test.
            (source_path / "marker.txt").write_text("consolidated")
            second_target = trigger_post_consolidation_snapshot(
                scheduler, "evolution", str(source_path)
            )
            assert second_target != first_target
            assert (
                scheduler.alias_manager.read_alias("evolution-global") == second_target
            )

            hnsw_loader_calls: list = []

            def _tracking_loader():
                hnsw_loader_calls.append(1)
                return _make_real_hnsw_index(), {}

            hnsw_cache.get_or_load(old_coll, _tracking_loader)
            assert hnsw_loader_calls, (
                "trigger_post_consolidation_snapshot() must invalidate "
                "the HNSW cache for the OLD snapshot."
            )

            # Staleness is checked per-key on direct re-access (not via a
            # proactive cross-key sweep) -- re-requesting the SAME old
            # key must return a genuinely NEW, uncached object.
            store_second = chunk_cache.get_or_open(old_db, old_coll)
            try:
                assert store_second is not store_first, (
                    "trigger_post_consolidation_snapshot() must "
                    "invalidate the ChunkStoreThreadCache for the OLD "
                    "snapshot -- a fifth real production alias-swap site."
                )
            finally:
                store_second.close()
        finally:
            # store_first is already closed by production code's own
            # stale-eviction branch (inside the store_second open above)
            # on the success path; sqlite3's Connection.close() tolerates
            # a repeat call, and this guarantees cleanup even if an
            # earlier assertion raised first.
            store_first.close()
