"""GitHub Bug #1775 remediation: `_post_provider_index_snapshot()`
(`code_indexer.server.mcp.handlers.repos`) never invalidated EITHER cache
before this fix. It is called after a provider-specific reindex job
creates a new versioned snapshot for an already-versioned repo (Bug #604)
-- a fourth real production alias-swap site, distinct from
`_cb_swap_alias()`, `RefreshScheduler._execute_refresh_impl()`, and
`GoldenRepoManager.add_indexes_to_golden_repo()`'s add-index publish.

Calls the real module-level function directly with a REAL
`RefreshScheduler` (real `AliasManager`; only `_create_snapshot` is
stubbed, matching the established pattern from the other sites' tests).
The scheduler reaches the function via the REAL external dependency it
reads from -- `app.state.global_lifecycle_manager` -- monkeypatched the
same way `test_golden_repo_cleanup_gate_bug1084.py`'s
`stub_cleanup_manager` fixture already does for the sibling
`_cb_swap_alias()` tests, rather than patching the internal
`_get_app_refresh_scheduler()` accessor itself. The real `swap_alias()`
call and the real cache singletons are exercised unmocked.
"""

from pathlib import Path
from types import SimpleNamespace

import hnswlib
import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server import app as app_module
from code_indexer.server.cache import get_global_cache, reset_global_cache
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
REPO_ALIAS = "myrepo-global"


def _make_real_hnsw_index() -> hnswlib.Index:
    idx = hnswlib.Index(space=HNSW_SPACE, dim=HNSW_DIM)
    idx.init_index(
        max_elements=HNSW_MAX_ELEMENTS, ef_construction=HNSW_EF_CONSTRUCTION, M=HNSW_M
    )
    return idx


def _make_versioned_snapshot(base: Path, repo: str, version: str, point_id: str):
    snapshot_dir = base / ".versioned" / repo / version
    collection_dir = snapshot_dir / INDEX_SUBPATH
    collection_dir.mkdir(parents=True, exist_ok=True)
    db_path = collection_dir / CHUNKS_DB_FILENAME
    store = ChunkStore(db_path)
    try:
        store.write_batch(
            [{"id": point_id, "vector": VECTOR, "payload": {"path": f"{point_id}.py"}}]
        )
    finally:
        store.close()
    return str(db_path), str(collection_dir), str(snapshot_dir)


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_global_cache()
    reset_global_chunk_store_cache()
    yield
    get_global_chunk_store_cache().close_current_thread()
    reset_global_cache()
    reset_global_chunk_store_cache()


def _build_real_scheduler(tmp_path: Path, old_dir: str, new_versioned: str):
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)

    query_tracker = QueryTracker()
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(),
        query_tracker=query_tracker,
        cleanup_manager=CleanupManager(query_tracker),
    )
    scheduler.alias_manager.create_alias(REPO_ALIAS, old_dir, repo_name="myrepo")
    # setattr (not direct assignment) for both lines below -- mypy flags
    # `scheduler._create_snapshot = ...` as reassigning a bound method
    # ([method-assign]), and `scheduler.cleanup_manager = None` as an
    # assignment-type mismatch against the CleanupManager-typed attribute.
    # setattr achieves the identical runtime effect without tripping
    # either check. cleanup_manager=None remains deliberate: it exercises
    # _post_provider_index_snapshot's getattr(scheduler, "cleanup_manager",
    # None)-safe branch.
    setattr(
        scheduler, "_create_snapshot", lambda alias_name, source_path: new_versioned
    )
    setattr(scheduler, "cleanup_manager", None)
    return scheduler, str(golden_repos_dir / "myrepo")


def _wire_scheduler_into_app_state(monkeypatch, scheduler) -> None:
    """Mirrors test_golden_repo_cleanup_gate_bug1084.py's
    stub_cleanup_manager fixture: wire a real object into the REAL
    external dependency (app.state.global_lifecycle_manager) the
    production accessor reads from, rather than patching the accessor
    function itself."""
    lifecycle = SimpleNamespace(refresh_scheduler=scheduler)
    monkeypatch.setattr(
        app_module.app.state, "global_lifecycle_manager", lifecycle, raising=False
    )


def _populate_both_caches(chunk_cache, hnsw_cache, old_db: str, old_coll: str):
    store_first = chunk_cache.get_or_open(old_db, old_coll)
    assert store_first.read("p1") is not None
    hnsw_cache.get_or_load(str(Path(old_coll)), lambda: (_make_real_hnsw_index(), {}))
    return store_first


def _assert_hnsw_evicted(hnsw_cache, old_coll: str) -> None:
    hnsw_loader_calls: list = []

    def _tracking_loader():
        hnsw_loader_calls.append(1)
        return _make_real_hnsw_index(), {}

    hnsw_cache.get_or_load(str(Path(old_coll)), _tracking_loader)
    assert hnsw_loader_calls, (
        "_post_provider_index_snapshot() must invalidate the HNSW cache "
        "for the OLD snapshot."
    )


def _assert_chunk_store_evicted(
    chunk_cache, old_db: str, old_coll: str, store_first
) -> None:
    # Staleness is checked per-key on direct re-access (not via a
    # proactive cross-key sweep) -- re-requesting the SAME old key must
    # return a genuinely NEW, uncached object. Closing store_first is the
    # CALLER's responsibility (guaranteed via the test method's own
    # try/finally) -- this helper only owns store_second's cleanup.
    store_second = chunk_cache.get_or_open(old_db, old_coll)
    try:
        assert store_second is not store_first, (
            "_post_provider_index_snapshot() must invalidate the "
            "ChunkStoreThreadCache for the OLD snapshot -- a fourth real "
            "production alias-swap site."
        )
    finally:
        store_second.close()


class TestPostProviderIndexSnapshotInvalidatesBothCaches1775:
    def test_invalidates_hnsw_and_chunk_store_caches_for_old_snapshot(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.mcp.handlers import repos as repos_module

        old_db, old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "myrepo", "v_1", "p1"
        )
        new_versioned = str(tmp_path / ".versioned" / "myrepo" / "v_2000000")

        scheduler, base_clone_path = _build_real_scheduler(
            tmp_path, old_dir, new_versioned
        )
        _wire_scheduler_into_app_state(monkeypatch, scheduler)

        chunk_cache = get_global_chunk_store_cache()
        hnsw_cache = get_global_cache()
        store_first = _populate_both_caches(chunk_cache, hnsw_cache, old_db, old_coll)

        try:
            repos_module._post_provider_index_snapshot(
                repo_alias=REPO_ALIAS,
                base_clone_path=base_clone_path,
                old_snapshot_path=old_dir,
            )

            # Prove the REAL swap actually ran.
            assert scheduler.alias_manager.read_alias(REPO_ALIAS) == new_versioned

            _assert_hnsw_evicted(hnsw_cache, old_coll)
            _assert_chunk_store_evicted(chunk_cache, old_db, old_coll, store_first)
        finally:
            # store_first is already closed by production code's own
            # stale-eviction branch (inside _assert_chunk_store_evicted's
            # store_second open) on the success path; sqlite3's
            # Connection.close() tolerates a repeat call, and this
            # guarantees cleanup even if an earlier assertion (e.g. the
            # HNSW one) raised first.
            store_first.close()
