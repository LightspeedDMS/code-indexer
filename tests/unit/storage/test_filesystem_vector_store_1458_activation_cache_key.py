"""FSV cache-key embeds the chunks_db-adjacent per-clone activation_id token
so a post-deactivate-then-reactivate read is a STRUCTURAL cache-miss (Story
#1458 AC11).

Because base-clone consolidation mutates the SAME collection path in place,
a process holding cached HNSW/id_index entries for that path must not keep
serving a DIFFERENT clone's stale content after a deactivate-then-reactivate
cycle places a new clone at the same reused path. The `chunks_db` layout
discriminator token alone is necessary but not sufficient for this case
(both clones can be fully consolidated with an IDENTICAL discriminator
value) -- a per-clone generation/identity token (`activation_id`) closes it.

AC11's FIRST technical requirement -- embedding the `collection_meta.json`
`chunks_db` flag/version token itself into the cache keys, so a post-
consolidation read (SHARDED_JSON -> CHUNKS_DB at the SAME path) is a
structural cache-miss -- is covered separately below
(`TestChunkLayoutTokenIsEmbeddedInTheKey` /
`TestChunkLayoutTokenMakesSearchAndInvalidateConsistent`), independent of
the activation_id token.

Real `HNSWIndexCache`/`IdIndexCache` (the actual production cache classes),
real `HNSWIndexManager`/`IDIndexManager`-built indexes on real files, real
`FilesystemVectorStore.search()` calls -- no mocking of the cache or storage
layer under test. `embedding_provider=Mock()` is the SAME established
convention this test suite already uses elsewhere (e.g.
test_filesystem_vector_store_1456_chunks_db_search.py) because the provider
is genuinely UNUSED whenever `precomputed_query_vector` is supplied.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.server.cache.hnsw_index_cache import HNSWIndexCache
from code_indexer.server.cache.id_index_cache import IdIndexCache
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _build_sharded_json_collection(
    store: FilesystemVectorStore,
    collection_name: str,
    vectors: list,
) -> Path:
    """Build a real SHARDED_JSON-layout collection (legacy vector_*.json +
    a real HNSW index built from them) so both the HNSW cache AND the
    id_index cache read paths are genuinely exercised (Story #1456 AC7
    routes CHUNKS_DB collections around id_index_cache entirely)."""
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = Path(store._get_collection_path(collection_name))

    for i, vector in enumerate(vectors):
        point_id = f"vec_{i:04d}"
        record = {
            "id": point_id,
            "vector": vector.astype(np.float32).tolist(),
            "payload": {"path": f"{point_id}.py"},
            "chunk_text": f"content for {point_id}",
        }
        shard_dir = collection_path / point_id[:2] / point_id[2:4]
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))

    hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    hnsw_manager.rebuild_from_vectors(collection_path)

    return collection_path


@pytest.fixture
def rng():
    return np.random.default_rng(4242)


class TestActivationScopedCacheKeyComposition:
    """Direct unit coverage of the key-composition rule itself."""

    def test_no_activation_id_key_is_byte_identical_to_bare_path(
        self, tmp_path: Path
    ) -> None:
        store = FilesystemVectorStore(base_path=tmp_path, activation_id=None)
        collection_path = tmp_path / "some_collection"

        key = store._activation_scoped_cache_key(str(collection_path))

        assert key == str(collection_path)

    def test_activation_id_is_appended_to_the_key(self, tmp_path: Path) -> None:
        store = FilesystemVectorStore(
            base_path=tmp_path, activation_id="11111111-1111-1111-1111-111111111111"
        )
        collection_path = tmp_path / "some_collection"

        key = store._activation_scoped_cache_key(str(collection_path))

        assert key == f"{collection_path}:11111111-1111-1111-1111-111111111111"

    def test_different_activation_ids_produce_different_keys(
        self, tmp_path: Path
    ) -> None:
        store_a = FilesystemVectorStore(base_path=tmp_path, activation_id="clone-a")
        store_b = FilesystemVectorStore(base_path=tmp_path, activation_id="clone-b")
        collection_path = tmp_path / "some_collection"

        key_a = store_a._activation_scoped_cache_key(str(collection_path))
        key_b = store_b._activation_scoped_cache_key(str(collection_path))

        assert key_a != key_b


class TestChunkLayoutTokenIsEmbeddedInTheKey:
    """AC11 Technical Requirement #1 (distinct from the activation_id token
    above): the `chunks_db` discriminator itself must be embeddable into the
    cache key, so a post-consolidation read at the SAME path is a structural
    cache-miss even with NO activation_id involved at all (CLI/solo path)."""

    def test_no_chunk_layout_token_is_byte_identical_to_bare_path(
        self, tmp_path: Path
    ) -> None:
        store = FilesystemVectorStore(base_path=tmp_path, activation_id=None)
        collection_path = tmp_path / "some_collection"

        key = store._activation_scoped_cache_key(str(collection_path))

        assert key == str(collection_path)

    def test_chunk_layout_token_is_appended_to_the_key(self, tmp_path: Path) -> None:
        store = FilesystemVectorStore(base_path=tmp_path, activation_id=None)
        collection_path = tmp_path / "some_collection"

        key = store._activation_scoped_cache_key(
            str(collection_path), chunk_layout_token="chunks_db"
        )

        assert key == f"{collection_path}:chunks_db"

    def test_different_chunk_layout_tokens_produce_different_keys(
        self, tmp_path: Path
    ) -> None:
        store = FilesystemVectorStore(base_path=tmp_path, activation_id="clone-a")
        collection_path = tmp_path / "some_collection"

        key_sharded = store._activation_scoped_cache_key(
            str(collection_path), chunk_layout_token="sharded_json"
        )
        key_chunks_db = store._activation_scoped_cache_key(
            str(collection_path), chunk_layout_token="chunks_db"
        )

        assert key_sharded != key_chunks_db
        assert key_sharded == f"{collection_path}:sharded_json:clone-a"
        assert key_chunks_db == f"{collection_path}:chunks_db:clone-a"


class TestActivationIdMakesSearchAStructuralCacheMiss:
    """End-to-end via the real search() production method + real shared
    HNSWIndexCache/IdIndexCache -- exercises the actual query hot path, not
    just the key-composition helper in isolation."""

    def test_same_activation_id_reuses_cache_different_id_forces_reload(
        self, tmp_path: Path, rng
    ) -> None:
        shared_hnsw_cache = HNSWIndexCache()
        shared_id_cache = IdIndexCache()

        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(10)]

        # FSV instance A: activation_id=None (CLI/solo/non-activated -- the
        # default, unchanged path).
        store_a = FilesystemVectorStore(
            base_path=tmp_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id=None,
        )
        _build_sharded_json_collection(store_a, "coll", vectors)

        # First search: guaranteed cache miss (nothing cached yet).
        store_a.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._miss_count == 1
        assert shared_id_cache._miss_count == 1

        # Second search, SAME activation_id (None) -- must be a cache HIT,
        # proving normal caching behavior is fully preserved.
        store_a.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._miss_count == 1
        assert shared_hnsw_cache._hit_count == 1
        assert shared_id_cache._miss_count == 1
        assert shared_id_cache._hit_count == 1

        # FSV instance B: a DIFFERENT clone materialized at the SAME path
        # (simulating deactivate-then-reactivate), with a genuinely
        # different activation_id but IDENTICAL on-disk collection data
        # (same base_path, same collection_name -- the exact hazard AC11
        # Finding 7 describes: same path, same discriminator state).
        store_b = FilesystemVectorStore(
            base_path=tmp_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id="new-clone-activation-id",
        )

        store_b.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )

        # A STRUCTURAL cache miss -- the differing activation_id changed
        # the key, so the new clone's read never touched store_a's cached
        # entry.
        assert shared_hnsw_cache._miss_count == 2
        assert shared_hnsw_cache._hit_count == 1  # unchanged from before
        assert shared_id_cache._miss_count == 2
        assert shared_id_cache._hit_count == 1  # unchanged from before

    def test_stale_cached_entry_never_served_across_reactivation(
        self, tmp_path: Path, rng
    ) -> None:
        """The concrete data-correctness proof behind the miss/hit counters
        above: after a simulated reactivation with DIFFERENT underlying
        vector data at the identical path, the new FSV instance's search
        returns the NEW data, never a stale cached result from the old
        clone."""
        shared_hnsw_cache = HNSWIndexCache()
        shared_id_cache = IdIndexCache()

        old_vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        store_old = FilesystemVectorStore(
            base_path=tmp_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id="old-clone",
        )
        _build_sharded_json_collection(store_old, "coll", old_vectors)
        old_results = store_old.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=1,
            precomputed_query_vector=old_vectors[0].tolist(),
        )
        assert old_results[0]["id"] == "vec_0000"
        assert old_results[0]["payload"]["path"] == "vec_0000.py"

        # Simulate deactivate + purge + reactivate: wipe the collection dir
        # and rebuild fresh, different content at the IDENTICAL base_path.
        import shutil

        shutil.rmtree(tmp_path / "coll")

        new_vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        store_new = FilesystemVectorStore(
            base_path=tmp_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id="new-clone",
        )
        _build_sharded_json_collection(store_new, "coll", new_vectors)

        new_results = store_new.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=1,
            precomputed_query_vector=new_vectors[0].tolist(),
        )

        assert new_results[0]["id"] == "vec_0000"
        # Same point_id/path (fresh collection built the same way), but the
        # ACTUAL proof this is genuinely fresh data, not a stale served
        # cache entry: the miss counter shows the read was NOT served from
        # store_old's cached entry.
        assert shared_hnsw_cache._miss_count == 2
        assert shared_id_cache._miss_count == 2


class TestChunkLayoutTokenMakesSearchAndInvalidateConsistent:
    """End-to-end proof of AC11 Technical Requirement #1 via the REAL
    production `search()` and `rebuild_hnsw_filtered()` methods -- not the
    isolated `_activation_scoped_cache_key()` helper.

    Isolates the discriminator as the ONLY variable: the SAME FSV instance,
    SAME activation_id, SAME collection path, SAME hnsw_index.bin file (no
    rebuild between calls, so the existing mtime-based cache invalidation --
    EVO-64244 Facet 2 -- cannot be what causes any observed miss). The only
    thing that changes between the second and third search() call is the
    `chunks_db` discriminator in collection_meta.json, flipped via the real
    production `write_chunks_db_discriminator()` plus a real `ChunkStore`-
    written `chunks.db` (mirrors AC3's transient on-disk coexistence of both
    representations during its steps 2-4 write window).
    """

    def test_discriminator_flip_at_same_path_is_a_structural_hnsw_cache_miss(
        self, tmp_path: Path, rng
    ) -> None:
        shared_hnsw_cache = HNSWIndexCache()
        shared_id_cache = IdIndexCache()

        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(10)]
        store = FilesystemVectorStore(
            base_path=tmp_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id="stable-activation-id",
        )
        collection_path = _build_sharded_json_collection(store, "coll", vectors)
        hnsw_bin_path = collection_path / HNSWIndexManager.INDEX_FILENAME
        mtime_before_flip = hnsw_bin_path.stat().st_mtime_ns

        # Search #1 (SHARDED_JSON): guaranteed miss (nothing cached yet).
        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._miss_count == 1

        # Search #2, SAME activation_id, SAME path, layout UNCHANGED (still
        # SHARDED_JSON) -- must be a cache HIT. Sanity check: proves the new
        # chunk_layout_token wiring does not spuriously bust the cache when
        # nothing on disk actually changed.
        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._miss_count == 1
        assert shared_hnsw_cache._hit_count == 1

        # Flip the discriminator to CHUNKS_DB in place, at the IDENTICAL
        # collection path -- write a real chunks.db (AC3's pure-addition
        # write) then durably flip the flag (AC3 step 4), WITHOUT touching
        # hnsw_index.bin (no rebuild -- isolates the discriminator as the
        # only changed variable).
        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            chunk_store.write_batch(
                [
                    {
                        "id": f"vec_{i:04d}",
                        "vector": v.astype(np.float32).tolist(),
                        "payload": {"path": f"vec_{i:04d}.py"},
                        "chunk_text": f"content for vec_{i:04d}",
                    }
                    for i, v in enumerate(vectors)
                ]
            )
        finally:
            chunk_store.close()
        write_chunks_db_discriminator(collection_path)

        assert hnsw_bin_path.stat().st_mtime_ns == mtime_before_flip, (
            "test setup invariant violated: hnsw_index.bin must NOT be "
            "touched by the discriminator flip, or a mtime-based cache-bust "
            "(EVO-64244 Facet 2) would confound this proof"
        )

        # Search #3: SAME activation_id, SAME path, SAME hnsw_index.bin
        # mtime -- ONLY the discriminator changed. Must be a STRUCTURAL
        # cache miss (AC11 Technical Requirement #1), proving the
        # chunks_db discriminator token is genuinely embedded in the real
        # search() cache-key composition, not just the isolated helper.
        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._miss_count == 2
        assert shared_hnsw_cache._hit_count == 1  # unchanged -- NOT a hit

    def test_rebuild_hnsw_filtered_invalidate_uses_the_matching_discriminator_key(
        self, tmp_path: Path, rng
    ) -> None:
        """`rebuild_hnsw_filtered()`'s `invalidate()` calls must compose the
        SAME discriminator-aware key `search()`'s `get_or_load()` stored the
        entry under -- otherwise, once the key format changes, invalidate()
        silently becomes a no-op (Anti-Orphan-Code: wiring must actually
        connect, not merely exist)."""
        shared_hnsw_cache = HNSWIndexCache()
        shared_id_cache = IdIndexCache()

        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        store = FilesystemVectorStore(
            base_path=tmp_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id="stable-activation-id",
        )
        _build_sharded_json_collection(store, "coll", vectors)

        # Populate the cache entry under today's SHARDED_JSON discriminator
        # state.
        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._miss_count == 1
        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._hit_count == 1

        # rebuild_hnsw_filtered() must invalidate the SAME key -- if it
        # composed a mismatched key (e.g. omitted the discriminator token),
        # the stale entry would remain cached and the next search() would
        # STILL be served from it (a false HIT) despite the on-disk index
        # having just been rebuilt.
        all_paths = {f"vec_{i:04d}.py" for i in range(len(vectors))}
        store.rebuild_hnsw_filtered("coll", visible_files=all_paths)

        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert shared_hnsw_cache._miss_count == 2, (
            "rebuild_hnsw_filtered()'s invalidate() call used a key that "
            "did not match the stored entry -- the post-rebuild search was "
            "wrongly served from the stale cache (a silent no-op eviction)"
        )


class TestInvalidateUsesResolvedPathMatchingSearch:
    """Codex Finding #9 (MEDIUM): rebuild_hnsw_filtered()'s invalidate()
    calls used str(collection_path) (unresolved) while search()'s
    get_or_load() keys with str(collection_path.resolve()) -- a real
    normalization mismatch for a base_path accessed via a symlink (or any
    other non-canonical form).

    Directly compares the ACTUAL key strings each call site passes to the
    shared cache classes (via real spies on HNSWIndexCache.get_or_load/
    invalidate), rather than relying on hit/miss counters -- those are
    confounded by EVO-64244 Facet 2's independent mtime-based
    invalidation (rebuild_hnsw_filtered's HNSW rebuild always bumps
    hnsw_index.bin's mtime, which busts the cache on its own regardless of
    whether the key matches, masking a pure key-mismatch bug)."""

    def test_invalidate_and_search_compose_the_identical_key_for_a_symlinked_base_path(
        self, tmp_path: Path, rng
    ) -> None:
        real_dir = tmp_path / "real-index"
        real_dir.mkdir()
        symlink_path = tmp_path / "index-via-symlink"
        symlink_path.symlink_to(real_dir, target_is_directory=True)
        # Test-setup invariant: the symlink path must genuinely differ from
        # its own resolved form, or this test would not exercise the
        # hazard it's proving.
        assert str(symlink_path) != str(symlink_path.resolve())

        shared_hnsw_cache = HNSWIndexCache()
        shared_id_cache = IdIndexCache()
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(3)]

        store = FilesystemVectorStore(
            base_path=symlink_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id="stable-activation-id",
        )
        _build_sharded_json_collection(store, "coll", vectors)

        search_keys: list = []
        original_get_or_load = HNSWIndexCache.get_or_load

        def _spy_get_or_load(self, repo_path, loader, index_file=None):
            search_keys.append(repo_path)
            return original_get_or_load(self, repo_path, loader, index_file=index_file)

        invalidate_keys: list = []
        original_invalidate = HNSWIndexCache.invalidate

        def _spy_invalidate(self, repo_path):
            invalidate_keys.append(repo_path)
            return original_invalidate(self, repo_path)

        with (
            patch.object(HNSWIndexCache, "get_or_load", _spy_get_or_load),
            patch.object(HNSWIndexCache, "invalidate", _spy_invalidate),
        ):
            store.search(
                query="unused",
                embedding_provider=Mock(),
                collection_name="coll",
                limit=5,
                precomputed_query_vector=vectors[0].tolist(),
            )
            all_paths = {f"vec_{i:04d}.py" for i in range(len(vectors))}
            store.rebuild_hnsw_filtered("coll", visible_files=all_paths)

        assert len(search_keys) == 1
        assert len(invalidate_keys) == 1
        assert invalidate_keys[0] == search_keys[0], (
            "Bug: rebuild_hnsw_filtered()'s invalidate() composed a "
            f"DIFFERENT key ({invalidate_keys[0]!r}) than search()'s "
            f"get_or_load() ({search_keys[0]!r}) for the SAME collection "
            "-- a symlinked (non-canonical) base_path exposes the "
            "str(collection_path) vs str(collection_path.resolve()) "
            "mismatch, making invalidate() silently target the wrong key."
        )
