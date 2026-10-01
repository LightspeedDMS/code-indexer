"""GitHub Bug #1775: GoldenRepoManager._cb_swap_alias() must invalidate the
ChunkStoreThreadCache singleton for the OLD (superseded) snapshot path,
mirroring the existing Bug #994 AC11 HNSW-cache eviction call in the same
function -- otherwise the fix landed in chunk_store_cache.py is INERT (the
same "registered but unwired" trap this project's CLAUDE.md documents for
Bug #1665: a correctly-built primitive with zero real call sites).

Fully real, non-mocked end-to-end: a real ``AliasManager`` (pre-seeded
alias file), a real ``GoldenRepoManager``, the real process-wide
``ChunkStoreThreadCache`` singleton, and real ``ChunkStore``/sqlite3-backed
chunk stores on disk. No mocking of the chunk-store cache or _cb_swap_alias
itself -- this proves the ACTUAL production code path, not a stand-in.
"""

import os
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.storage.shared.chunk_store_cache import (
    get_global_chunk_store_cache,
    reset_global_chunk_store_cache,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR = [0.1, 0.2, 0.3, 0.4]
CHUNKS_DB_FILENAME = "chunks.db"
PROVIDER_DIR = "voyage-code-3"
INDEX_SUBPATH = Path(".code-indexer") / "index" / PROVIDER_DIR


def _make_versioned_snapshot(base: Path, repo: str, version: str, point_id: str):
    """Create a real, valid chunk store at a canonical
    ``.versioned/<repo>/<version>/.code-indexer/index/<provider>/chunks.db``
    path. Returns (db_path_str, collection_path_str, snapshot_dir_str).
    """
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


def _make_manager(tmp_path: Path) -> GoldenRepoManager:
    return GoldenRepoManager(data_dir=str(tmp_path))


class TestChunkStoreCacheEvictionWiring1775:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        reset_global_chunk_store_cache()
        yield
        reset_global_chunk_store_cache()

    def test_swap_alias_invalidates_chunk_store_cache_for_old_snapshot(self, tmp_path):
        mgr = _make_manager(tmp_path)

        old_db, old_coll, old_dir = _make_versioned_snapshot(
            tmp_path, "myrepo", "v_1", "p1"
        )
        _new_db, _new_coll, new_dir = _make_versioned_snapshot(
            tmp_path, "myrepo", "v_2", "p2"
        )

        cache = get_global_chunk_store_cache()
        # Populate the OLD snapshot's entry exactly as a real query would
        # while serving it, on this thread.
        store_first = cache.get_or_open(old_db, old_coll)
        try:
            assert store_first.read("p1") is not None

            # Pre-seed the alias so _cb_swap_alias()'s read_alias() call
            # sees old_dir as the CURRENT target -- matching the
            # established bug1084 test pattern for exercising this method
            # directly.
            aliases_dir = os.path.join(mgr.golden_repos_dir, "aliases")
            os.makedirs(aliases_dir, exist_ok=True)
            alias_manager = AliasManager(aliases_dir)
            alias_manager.create_alias("myrepo-global", old_dir, repo_name="myrepo")

            # The real production wiring point.
            mgr._cb_swap_alias("myrepo", new_dir)

            # Re-accessing the SAME old key directly must return a
            # genuinely NEW, uncached object -- proving invalidate_prefix()
            # was genuinely invoked with old_dir by _cb_swap_alias()
            # itself, not skipped. (Staleness is checked per-key, not via
            # a proactive cross-key sweep -- see chunk_store_cache.py's
            # module docstring.)
            store_second = cache.get_or_open(old_db, old_coll)
            try:
                assert store_second is not store_first, (
                    "_cb_swap_alias() must invalidate the "
                    "ChunkStoreThreadCache singleton for the OLD snapshot "
                    "path -- without this wiring the chunk_store_cache.py "
                    "fix is inert (same 'registered but unwired' trap as "
                    "Bug #1665)."
                )
            finally:
                store_second.close()
        finally:
            # Defensive/explicit cleanup: by this point production code's
            # own stale-eviction branch has already closed store_first
            # internally (when store_second was opened) -- sqlite3's
            # Connection.close() tolerates being called twice, so this is
            # a safe no-op in the success path and real cleanup if the
            # test fails before reaching that point.
            store_first.close()
