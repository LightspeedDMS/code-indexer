"""Story #1492 AC1: real-concurrency throughput proof of the GIL reduction.

Report Finding C1: the redundant collection_meta.json re-parses are
GIL-HELD CPU-bound work (json.loads), so they directly limit how many
concurrent queries a server worker can actually service in parallel --
CPython's GIL means N threads doing 4-5x redundant json.loads() each
serialize almost completely, no matter how many CPU cores are available.

This test makes the per-parse cost large enough to measure (a synthetic
bloat field added ALONGSIDE the real, untouched hnsw_index metadata --
never replacing it, so the real HNSW/id_mapping data stays fully correct
and this test measures cache/parse behavior, not a metadata-consistency
artifact) and drives REAL concurrent search() calls with REAL
threading.ThreadPoolExecutor threads (never a mock) against a real
on-disk collection, comparing two genuinely different cache-sharing
configurations:

- UNCACHED: each thread gets its OWN fresh CollectionMetaCache (no
  cross-thread/cross-call reuse possible) -- every one of the N concurrent
  search() calls pays a full real parse.
- SHARED: all N concurrent search() calls share ONE CollectionMetaCache --
  TTLCache's single-flight guarantee (Story #1082) means concurrent
  misses for the IDENTICAL key -- guaranteed here since the file's mtime
  never changes mid-run -- coalesce to EXACTLY ONE real parse; the
  remaining N-1 calls are cache hits.

Two independent proofs, deterministic first, timing second:
1. Deterministic: the SHARED cache's own reload counter (TTLCache's real
   bookkeeping, Story #1082's public API) is asserted to be exactly 1
   after all N concurrent calls complete -- not inferred from timing.
2. Measured wall-clock: median-of-several-trials comparison showing the
   SHARED run is meaningfully faster under real concurrent load, the
   direct observable consequence of proof #1.
"""

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.collection_meta_cache import CollectionMetaCache

VECTOR_DIM = 16
NUM_CONCURRENT_SEARCHES = 8
NUM_TIMING_TRIALS = 3
# Large enough that json.loads() of the WHOLE document is measurably slow
# (tens of ms), mirroring the report's real-world large collection_meta.json
# files (p90 1.06 MB, max 56.3 MB) without requiring an actual multi-MB
# fixture on every CI run. Stored under an UNRELATED top-level key -- never
# touching the real hnsw_index/id_mapping data, so the collection's actual
# search behavior/correctness is completely unaffected.
SYNTHETIC_BLOAT_ENTRIES = 60_000


class _NeverInvokedEmbeddingProvider:
    """Explicitly documents (and enforces) that this call path must never
    invoke the embedding provider: every search() call below supplies
    precomputed_query_vector, which is documented (search()'s own
    docstring) to skip generate_embedding()/coalesced_query_embedding()
    entirely. Raises loudly, rather than silently doing nothing, if that
    contract is ever violated by a future change."""

    def get_embedding(self, *args, **kwargs):
        raise AssertionError(
            "embedding provider was invoked despite precomputed_query_vector "
            "being supplied -- search()'s precomputed-vector contract was "
            "violated"
        )

    def get_provider_name(self):
        raise AssertionError(
            "embedding provider was invoked despite precomputed_query_vector "
            "being supplied -- search()'s precomputed-vector contract was "
            "violated"
        )


def _build_bloated_collection(tmp_path: Path):
    """A real, small SHARDED_JSON collection whose collection_meta.json
    carries an additional, UNRELATED synthetic bloat field so a real parse
    is slow enough to measure under concurrency -- the real hnsw_index
    metadata (including id_mapping) is left completely untouched."""
    store = FilesystemVectorStore(
        base_path=tmp_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = Path(store._get_collection_path("coll"))

    rng = np.random.default_rng(1492)
    vector = rng.standard_normal(VECTOR_DIM)
    record = {
        "id": "vec_0000",
        "vector": vector.astype(np.float32).tolist(),
        "payload": {"path": "vec_0000.py"},
        "chunk_text": "content for vec_0000",
    }
    shard_dir = collection_path / "ve" / "c_"
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "vector_vec_0000.json").write_text(json.dumps(record))

    HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine").rebuild_from_vectors(
        collection_path
    )

    meta_path = collection_path / "collection_meta.json"
    meta = json.loads(meta_path.read_text())
    # Sibling of "hnsw_index"/"vector_size" -- never read by any consumer,
    # purely to bloat json.loads() cost. The real hnsw_index dict (id
    # mapping, is_stale, vector_count, etc.) is untouched.
    meta["_synthetic_bloat_for_test"] = {
        str(i): f"synthetic_padding_value_{i:08d}"
        for i in range(SYNTHETIC_BLOAT_ENTRIES)
    }
    meta_path.write_text(json.dumps(meta))

    return collection_path, vector


def _run_concurrent_searches(tmp_path: Path, query_vector, *, shared_cache) -> float:
    """Run NUM_CONCURRENT_SEARCHES real concurrent search() calls, each
    against its own FilesystemVectorStore instance pointed at the SAME
    on-disk collection. Returns wall-clock seconds for the whole batch.

    shared_cache: a CollectionMetaCache instance to share across every
    concurrent call (cross-thread reuse), or None to give each call its
    OWN fresh CollectionMetaCache (no reuse possible).
    """

    def make_store() -> FilesystemVectorStore:
        cache = shared_cache if shared_cache is not None else CollectionMetaCache()
        return FilesystemVectorStore(
            base_path=tmp_path,
            collection_meta_cache=cache,
            use_chunks_db_for_new_collections=False,
        )

    def one_search() -> int:
        store = make_store()
        results = store.search(
            query="unused",
            embedding_provider=_NeverInvokedEmbeddingProvider(),
            collection_name="coll",
            limit=1,
            precomputed_query_vector=query_vector.tolist(),
        )
        return len(results)

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=NUM_CONCURRENT_SEARCHES) as executor:
        futures = [executor.submit(one_search) for _ in range(NUM_CONCURRENT_SEARCHES)]
        for f in futures:
            f.result()
    return time.perf_counter() - start


def _reload_total(cache: CollectionMetaCache) -> int:
    counters = cache.counters()
    return int(counters["immutable"]["reload"] + counters["mutable"]["reload"])


class TestConcurrentSearchThroughput:
    def test_shared_cache_coalesces_to_exactly_one_real_parse(self, tmp_path):
        """Deterministic proof (proof #1): TTLCache's single-flight
        guarantee means N concurrent misses for the IDENTICAL
        (collection_dir, mtime) key coalesce to exactly one real parse."""
        _collection_path, query_vector = _build_bloated_collection(tmp_path)
        shared_cache = CollectionMetaCache()

        _run_concurrent_searches(tmp_path, query_vector, shared_cache=shared_cache)

        assert _reload_total(shared_cache) == 1

    def test_shared_cache_reduces_concurrent_wall_clock_time(self, tmp_path):
        """Measured proof (proof #2): the direct observable consequence of
        proof #1 -- fewer real (GIL-held) parses means faster real
        concurrent throughput. Median of several trials, generous margin,
        to avoid CI timing flakiness while still requiring a genuine,
        non-coincidental improvement."""
        collection_path, query_vector = _build_bloated_collection(tmp_path)
        # Warm the filesystem page cache once (avoid measuring disk-read
        # variance rather than json.loads() cost).
        (collection_path / "collection_meta.json").read_text()

        uncached_trials = [
            _run_concurrent_searches(tmp_path, query_vector, shared_cache=None)
            for _ in range(NUM_TIMING_TRIALS)
        ]
        shared_trials = [
            _run_concurrent_searches(
                tmp_path, query_vector, shared_cache=CollectionMetaCache()
            )
            for _ in range(NUM_TIMING_TRIALS)
        ]

        uncached_median = statistics.median(uncached_trials)
        shared_median = statistics.median(shared_trials)

        assert shared_median < uncached_median * 0.7, (
            f"expected shared-cache concurrent search to be meaningfully "
            f"faster (median of {NUM_TIMING_TRIALS} trials): "
            f"uncached={uncached_median:.4f}s shared={shared_median:.4f}s "
            f"(uncached trials={uncached_trials}, shared trials={shared_trials})"
        )
