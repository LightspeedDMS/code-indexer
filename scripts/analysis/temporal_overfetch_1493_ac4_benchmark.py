#!/usr/bin/env python3
"""Story #1493 AC4: committed, reproducible decode-volume + latency +
concurrency microbenchmark.

Builds a REAL CHUNKS_DB collection (real SQLite ChunkStore, real HNSW
index, real zstd-compressed payloads sized like real commit diffs)
reproducing the report's worst case: chunk_type=commit_message, limit=10,
~1,080 candidate rows per shard (2.7% head / 97.3% non-head skew, 30
commits x 36 chunks/commit -- 1 head + 35 non-head each).

Two subcommands:

  decode      Measures ChunkStore.read() call count and wall-clock time for
              FilesystemVectorStore.search(), single query, "before"
              (pre-#1493, no temporal_chunk_type) vs "after" (Story #1493
              AC2's decode skip active).

  concurrency Fires N real, concurrent FilesystemVectorStore.search() calls
              via a real ThreadPoolExecutor (genuine OS threads, genuine
              GIL contention -- not mocked), measuring TOTAL wall-clock
              time for the batch, "before" vs "after".

Both subcommands run BOTH "before" and "after" modes in one invocation and
report the comparison directly -- no separate before/after process
launches needed.

Usage:
    python3 temporal_overfetch_1493_ac4_benchmark.py decode <pythonpath> \\
        [--output result.json]
    python3 temporal_overfetch_1493_ac4_benchmark.py concurrency \\
        <pythonpath> <num_concurrent> [--output result.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

VECTOR_DIM = 1024  # matches real voyage-code-3 dimension
NUM_COMMITS = 30  # 2.7% of ~1,080 candidates ~= 30 head chunks
CHUNKS_PER_COMMIT = 36  # 1 head + 35 non-head -> ~2.7% head skew, ~1,080 total
RNG_SEED = 7
BASE_COMMIT_TIMESTAMP = 1700000000
FILTER_TS_LOW = 0
FILTER_TS_HIGH = 9999999999

# Mirrors what query_temporal() actually passes as FSV's `limit` kwarg --
# the per-shard SEARCH_LIMIT (already multiplied by both the shard-level
# and chunk-type overfetch factors), NOT the small user-facing display
# limit (that truncation happens LATER, in query_temporal's own
# post-filter/sort/[:limit] step, after FSV returns). true_user_limit=10,
# shard_multiplier=3, chunk_type_multiplier=40 -> natural=1200 ("before",
# uncapped); AC1's ceiling caps this to true_user_limit*60=600 ("after").
#
# IMPORTANT: these values (600/1200) are also FSV's OWN `limit` kwarg, which
# gates the lazy_load early-exit (`len(_res) >= limit`). Since only
# NUM_COMMITS (30) head chunks exist in the WHOLE corpus, that early-exit
# condition (>= 600) can never fire in "after" mode -- every one of the
# ~1,080 candidates is scanned via the zero-I/O point_id check, and only
# the ~30 that parse as head chunks are ever passed to ChunkStore.read().
# This benchmark's "after" measurement is NOT an artifact of stopping
# early at some small display limit; it genuinely scans the whole
# candidate pool and skips decode for the ~97% that are provably non-head.
NATURAL_SEARCH_LIMIT = 1200
CEILING_CAPPED_SEARCH_LIMIT = 600

REALISTIC_DIFF_TEXT = (
    "diff --git a/src/module.py b/src/module.py\n"
    "@@ -10,7 +10,7 @@ def some_function():\n"
    + ("    some_realistic_line_of_code_here_padding_text\n" * 40)
)


def _generate_test_records(
    build_point_id_fn: Any, np_module: Any, rng: Any
) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """Generate the ~1,080 synthetic records (2.7% head / 97.3% non-head)
    and their raw vectors.

    Args are typed `Any` deliberately: `build_point_id_fn`/`np_module` are
    resolved dynamically inside `_build_test_collection` via `sys.path`
    manipulation against a CALLER-SUPPLIED pythonpath (so this same script
    can run against either the pre-#1493 or post-#1493 source tree) --
    importing them at module scope for static typing would defeat that
    design entirely.
    """
    total_vectors = NUM_COMMITS * CHUNKS_PER_COMMIT
    vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(total_vectors)]
    records: List[Dict[str, Any]] = []
    idx = 0
    for commit_i in range(NUM_COMMITS):
        commit_hash = f"hash{commit_i:04d}"
        for chunk_index in range(CHUNKS_PER_COMMIT):
            point_id = build_point_id_fn("proj", commit_hash, chunk_index)
            records.append(
                {
                    "id": point_id,
                    "vector": vectors[idx].astype(np_module.float32).tolist(),
                    "payload": {
                        "path": f"{commit_hash}.py",
                        "is_head": chunk_index == 0,
                        "commit_timestamp": BASE_COMMIT_TIMESTAMP + commit_i,
                    },
                    "chunk_text": REALISTIC_DIFF_TEXT,
                }
            )
            idx += 1
    return records, vectors


def _write_and_index_collection(
    collection_path: Path,
    records: List[Dict[str, Any]],
    chunk_store_cls: Any,
    write_discriminator_fn: Any,
    hnsw_manager_cls: Any,
) -> None:
    """Same dynamic-import rationale as _generate_test_records above."""
    chunk_store = chunk_store_cls(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()
    write_discriminator_fn(collection_path)
    hnsw_manager_cls(vector_dim=VECTOR_DIM, space="cosine").rebuild_from_vectors(
        collection_path
    )


def _build_test_collection(
    pythonpath: str, tmp_path: Path
) -> Tuple[Any, List[Any], int]:
    """Build a real CHUNKS_DB collection reproducing the report's worst
    case. Returns (store, vectors, total_vectors). All writer calls
    (create_collection/write_batch/discriminator/rebuild) are the same
    void-returning setup primitives this project's own unit tests
    (test_filesystem_vector_store_1456_chunks_db_search.py) call without
    checking a return value -- there is nothing to validate."""
    sys.path.insert(0, pythonpath)
    import numpy as np

    from code_indexer.services.temporal.temporal_point_builder import build_point_id
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
    from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
    from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    rng = np.random.default_rng(RNG_SEED)
    records, vectors = _generate_test_records(build_point_id, np, rng)
    total_vectors = len(records)

    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = Path(store._get_collection_path("coll"))

    _write_and_index_collection(
        collection_path,
        records,
        ChunkStore,
        write_chunks_db_discriminator,
        HNSWIndexManager,
    )
    return store, vectors, total_vectors


def _search_kwargs_for_mode(
    mode: str, vectors: List[Any], total_vectors: int
) -> Dict[str, Any]:
    unused_embedding_provider_placeholder = object()
    filter_conditions = {
        "must": [
            {
                "key": "commit_timestamp",
                "range": {"gte": FILTER_TS_LOW, "lte": FILTER_TS_HIGH},
            }
        ]
    }
    fsv_limit = CEILING_CAPPED_SEARCH_LIMIT if mode == "after" else NATURAL_SEARCH_LIMIT
    kwargs: Dict[str, Any] = dict(
        query="unused",
        embedding_provider=unused_embedding_provider_placeholder,
        collection_name="coll",
        limit=fsv_limit,
        filter_conditions=filter_conditions,
        precomputed_query_vector=vectors[0].tolist(),
        lazy_load=True,
        prefetch_limit=total_vectors,
    )
    if mode == "after":
        kwargs["temporal_chunk_type"] = "commit_message"
    return kwargs


def run_decode_benchmark(pythonpath: str) -> Dict[str, Any]:
    from unittest.mock import patch

    sys.path.insert(0, pythonpath)
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    results: Dict[str, Any] = {}
    for mode in ("before", "after"):
        with tempfile.TemporaryDirectory() as tmp:
            store, vectors, total_vectors = _build_test_collection(
                pythonpath, Path(tmp)
            )
            search_kwargs = _search_kwargs_for_mode(mode, vectors, total_vectors)
            with patch.object(
                ChunkStore, "read", autospec=True, side_effect=ChunkStore.read
            ) as read_spy:
                t0 = time.perf_counter()
                search_results = store.search(**search_kwargs)
                elapsed_ms = (time.perf_counter() - t0) * 1000
            results[mode] = {
                "total_candidates": total_vectors,
                "chunk_store_read_calls": read_spy.call_count,
                "results_returned": len(search_results),
                # Whole search() call (HNSW query + hydration together) --
                # isolating hydration alone would require adding internal
                # instrumentation to production code, which this
                # benchmark deliberately avoids.
                "search_elapsed_ms": elapsed_ms,
            }
    return results


def run_concurrency_benchmark(pythonpath: str, num_concurrent: int) -> Dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor

    results: Dict[str, Any] = {}
    for mode in ("before", "after"):
        with tempfile.TemporaryDirectory() as tmp:
            store, vectors, total_vectors = _build_test_collection(
                pythonpath, Path(tmp)
            )
            search_kwargs = _search_kwargs_for_mode(mode, vectors, total_vectors)

            def _do_one_search() -> Any:
                return store.search(**search_kwargs)

            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
                futures = [
                    executor.submit(_do_one_search) for _ in range(num_concurrent)
                ]
                batch_results = [f.result() for f in futures]
            elapsed_ms = (time.perf_counter() - t0) * 1000
            results[mode] = {
                "num_concurrent": num_concurrent,
                "total_wall_ms": elapsed_ms,
                "per_query_avg_ms": elapsed_ms / num_concurrent,
                "results_per_query": len(batch_results[0]),
            }
    return results


def _validate_output_path(output: Path) -> None:
    if output.suffix != ".json":
        raise SystemExit(f"--output must end in .json, got: {output}")
    if not output.parent.exists():
        raise SystemExit(f"--output parent directory does not exist: {output.parent}")


def cmd_decode(args: argparse.Namespace) -> int:
    results = run_decode_benchmark(args.pythonpath)
    print(json.dumps(results, indent=2))
    if args.output:
        _validate_output_path(args.output)
        args.output.write_text(json.dumps(results, indent=2))
    return 0


def cmd_concurrency(args: argparse.Namespace) -> int:
    results = run_concurrency_benchmark(args.pythonpath, args.num_concurrent)
    print(json.dumps(results, indent=2))
    if args.output:
        _validate_output_path(args.output)
        args.output.write_text(json.dumps(results, indent=2))
    return 0


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got: {value!r}")
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"must be positive, got: {parsed}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    decode_parser = subparsers.add_parser("decode")
    decode_parser.add_argument("pythonpath", type=str)
    decode_parser.add_argument("--output", type=Path, default=None)
    decode_parser.set_defaults(func=cmd_decode)

    concurrency_parser = subparsers.add_parser("concurrency")
    concurrency_parser.add_argument("pythonpath", type=str)
    concurrency_parser.add_argument("num_concurrent", type=_positive_int)
    concurrency_parser.add_argument("--output", type=Path, default=None)
    concurrency_parser.set_defaults(func=cmd_concurrency)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
