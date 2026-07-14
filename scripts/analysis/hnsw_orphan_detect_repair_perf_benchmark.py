#!/usr/bin/env python3
"""
Story #1359 (Epic #1333, S2) AC5: perf guardrail benchmark.

Standalone operator benchmark (NOT automated CI, matches the
scripts/analysis/multi_worker_throughput.py precedent) measuring the added
cost of the finalize-time orphan detect+repair hook
(HNSWIndexManager._detect_and_repair_orphans) at production HNSW build
parameters (M=16, ef_construction=200, cosine).

Two measurements:

  1. Detection overhead at scale: build a large (default 200k), diverse,
     healthy (0-orphan) corpus and measure check_integrity()'s wall-clock
     cost as a percentage of the add_items() build cost. This is the cost
     EVERY finalize pays, healthy or not (AC5: must be < 1% of build time).

  2. Repair cost independence from index size: build a large diverse corpus
     with a SMALL embedded near-tie pocket (orphan count bounded by pocket
     size, not by the surrounding corpus size) and measure repair_orphans()
     wall-clock time. Recorded as a benchmark note, not a pass/fail gate --
     the spike's own finding is that repair cost scales with orphan count,
     not index size.

Usage:
    PYTHONPATH=./src python3 scripts/analysis/hnsw_orphan_detect_repair_perf_benchmark.py \\
        [--size 200000] [--dim 1024] [--pocket-size 150]

Real project hnswlib fork only. No mocks.
"""

import argparse
import time

import numpy as np


def _human(seconds: float) -> str:
    return f"{seconds:.3f}s"


def benchmark_detection_overhead(size: int, dim: int) -> None:
    import hnswlib

    if size <= 0 or dim <= 0:
        raise ValueError(f"size and dim must be positive, got size={size} dim={dim}")

    print(f"\n=== Detection overhead at scale (size={size}, dim={dim}) ===")
    rng = np.random.RandomState(42)
    vectors = rng.randn(size, dim).astype(np.float32)

    index = hnswlib.Index(space="cosine", dim=dim)
    index.init_index(
        max_elements=size, M=16, ef_construction=200, allow_replace_deleted=True
    )
    labels = np.arange(size)

    t0 = time.time()
    index.add_items(vectors, labels)
    build_time = time.time() - t0

    t1 = time.time()
    integrity = index.check_integrity()
    check_time = time.time() - t1

    orphan_count = sum(1 for e in integrity["errors"] if "orphan" in e)
    ratio_pct = (check_time / build_time) * 100 if build_time > 0 else 0.0

    print(f"  add_items (build) time:   {_human(build_time)}")
    print(f"  check_integrity() time:   {_human(check_time)}")
    print(f"  orphan_count:             {orphan_count} (expected 0 for diverse corpus)")
    print(f"  overhead ratio:           {ratio_pct:.4f}% of build time")
    print(f"  AC5 requirement (<1%):    {'PASS' if ratio_pct < 1.0 else 'FAIL'}")


def benchmark_repair_cost_independence(size: int, dim: int, pocket_size: int) -> None:
    import sys
    from pathlib import Path

    if size <= 0 or dim <= 0:
        raise ValueError(f"size and dim must be positive, got size={size} dim={dim}")
    if pocket_size <= 0 or pocket_size >= size:
        raise ValueError(
            f"pocket_size must be in (0, size), got pocket_size={pocket_size} size={size}"
        )

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from utils.hnsw_orphan_corpus import build_hnsw_index, near_tie_corpus

    print(
        f"\n=== Repair cost independence from index size "
        f"(size={size}, pocket_size={pocket_size}) ==="
    )
    pocket_fraction = pocket_size / size
    vectors = near_tie_corpus(
        size=size,
        dim=dim,
        noise_scale=1e-6,
        pocket_fraction=pocket_fraction,
        seed=42,
    )
    index = build_hnsw_index(vectors, num_threads=1)

    before = index.check_integrity()
    orphans_before = sum(1 for e in before["errors"] if "orphan" in e)

    t0 = time.time()
    repair_result = index.repair_orphans()
    repair_time = time.time() - t0

    print(f"  index size:               {size}")
    print(f"  embedded pocket size:     {pocket_size}")
    print(f"  orphans_before:           {orphans_before}")
    print(f"  orphans_after:            {repair_result['orphans_after']}")
    print(f"  repair_orphans() time:    {_human(repair_time)}")
    print(
        "  note: repair cost is bounded by orphan/pocket count, not overall "
        "index size (spike finding) -- run at multiple `size` values with a "
        "fixed pocket_size to observe repair_time staying roughly constant."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=200_000)
    parser.add_argument("--dim", type=int, default=1024)
    parser.add_argument("--pocket-size", type=int, default=150)
    args = parser.parse_args()

    if args.size <= 0:
        parser.error("--size must be positive")
    if args.dim <= 0:
        parser.error("--dim must be positive")
    if args.pocket_size <= 0 or args.pocket_size >= args.size:
        parser.error("--pocket-size must be positive and less than --size")

    benchmark_detection_overhead(args.size, args.dim)
    benchmark_repair_cost_independence(args.size, args.dim, args.pocket_size)


if __name__ == "__main__":
    main()
