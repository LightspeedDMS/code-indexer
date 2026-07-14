"""
Bug #1394: bounded-concurrency batch health-check helper.

GET /api/repositories/{alias}/health iterates every collection directory
serially, calling the SYNCHRONOUS HNSWHealthService.check_health() inside an
`async def` FastAPI route -- blocking the event loop for the whole duration.
On a golden repo with dozens of quarterly temporal shards this exceeds the
reverse-proxy timeout -> HTTP 504. There is also no per-collection exception
isolation: one corrupt/unreadable collection currently blows up the whole
aggregation.

check_health_batch() fixes both:
  1. Bounded concurrency via a dedicated ThreadPoolExecutor(max_workers=4)
     (small on purpose -- each load_index() transiently holds a whole shard
     in RAM).
  2. Per-collection exception isolation -- one path raising never corrupts
     or omits any other path's result in the returned dict.

Real threads, real on-disk hnswlib indexes -- no mocking of check_health
itself and no mocking of ThreadPoolExecutor.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List

import hnswlib
import numpy as np
import pytest

from code_indexer.services.hnsw_health_service import (
    HealthCheckResult,
    HNSWHealthService,
    check_health_batch,
)

DIM = 16


def _build_real_index(path: Path, num_elements: int = 20) -> None:
    """Build and save a small, genuinely valid on-disk HNSW index."""
    rng = np.random.RandomState(1)
    vectors = rng.randn(num_elements, DIM).astype(np.float32)

    index = hnswlib.Index(space="l2", dim=DIM)
    index.init_index(max_elements=num_elements, ef_construction=100, M=8)
    index.add_items(vectors, np.arange(num_elements))
    index.save_index(str(path))


class TestCheckHealthBatchBoundedConcurrency:
    """Prove real threads are used and concurrency is capped at max_workers."""

    def test_concurrency_never_exceeds_max_workers(self, tmp_path: Path):
        max_workers = 2
        num_indexes = 6

        index_paths: List[str] = []
        for i in range(num_indexes):
            p = tmp_path / f"collection-{i}" / "hnsw_index.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            _build_real_index(p)
            index_paths.append(str(p))

        service = HNSWHealthService(cache_ttl_seconds=300)

        active_count = 0
        max_observed = 0
        samples_lock = threading.Lock()
        real_perform_check = service._perform_check

        def instrumented_perform_check(index_path: str) -> HealthCheckResult:
            nonlocal active_count, max_observed
            with samples_lock:
                active_count += 1
                max_observed = max(max_observed, active_count)
            try:
                # Small sleep widens the window so overlapping calls are
                # actually observed concurrently rather than by chance.
                time.sleep(0.05)
                return real_perform_check(index_path)
            finally:
                with samples_lock:
                    active_count -= 1

        service._perform_check = instrumented_perform_check  # type: ignore[method-assign]

        results = check_health_batch(
            service, index_paths, force_refresh=True, max_workers=max_workers
        )

        assert len(results) == num_indexes
        assert max_observed <= max_workers, (
            f"Observed {max_observed} concurrent in-flight checks, "
            f"exceeding max_workers={max_workers}"
        )
        assert max_observed >= 2, (
            "Expected genuine concurrency (>=2 overlapping checks) to prove "
            "real threads are used, not serialized execution"
        )
        for path in index_paths:
            assert results[path].valid is True


class TestCheckHealthBatchPartialFailureIsolation:
    """One corrupt collection's check raising must not affect the others."""

    def test_one_corrupt_index_does_not_corrupt_sibling_results(self, tmp_path: Path):
        healthy_paths = []
        for i in range(3):
            p = tmp_path / f"healthy-{i}" / "hnsw_index.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            _build_real_index(p)
            healthy_paths.append(str(p))

        # Corrupt: file exists but is not a valid hnswlib index, so
        # hnswlib.Index.load_index() genuinely raises inside _perform_check.
        corrupt_path = tmp_path / "corrupt" / "hnsw_index.bin"
        corrupt_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_path.write_bytes(b"not a real hnsw index file")

        service = HNSWHealthService(cache_ttl_seconds=300)

        # Force _perform_check to raise for the corrupt path specifically,
        # proving the isolation contract even for exceptions that escape
        # _perform_check's own internal try/except (belt and suspenders --
        # the real hnswlib load_index() call above already fails naturally
        # too, but we also assert the harness-level contract directly).
        real_perform_check = service._perform_check

        def flaky_perform_check(index_path: str) -> HealthCheckResult:
            if index_path == str(corrupt_path):
                raise RuntimeError("simulated hard failure loading index")
            return real_perform_check(index_path)

        service._perform_check = flaky_perform_check  # type: ignore[method-assign]

        all_paths = healthy_paths + [str(corrupt_path)]
        results = check_health_batch(
            service, all_paths, force_refresh=True, max_workers=4
        )

        assert len(results) == len(all_paths)

        # Corrupt path: synthesized error result, not an escaped exception.
        corrupt_result = results[str(corrupt_path)]
        assert corrupt_result.valid is False
        assert corrupt_result.index_path == str(corrupt_path)
        assert len(corrupt_result.errors) > 0
        assert any(
            "simulated hard failure" in e or "raised" in e.lower()
            for e in corrupt_result.errors
        )

        # Healthy paths: unaffected, still correct.
        for path in healthy_paths:
            assert results[path].valid is True
            assert results[path].index_path == path

    def test_returns_dict_keyed_by_index_path_one_entry_per_input(self, tmp_path: Path):
        paths = []
        for i in range(3):
            p = tmp_path / f"coll-{i}" / "hnsw_index.bin"
            p.parent.mkdir(parents=True, exist_ok=True)
            _build_real_index(p)
            paths.append(str(p))

        service = HNSWHealthService(cache_ttl_seconds=300)
        results = check_health_batch(service, paths, force_refresh=True)

        assert isinstance(results, dict)
        assert set(results.keys()) == set(paths)


class TestCheckHealthBatchEmptyInput:
    def test_empty_index_paths_returns_empty_dict(self):
        service = HNSWHealthService(cache_ttl_seconds=300)
        results = check_health_batch(service, [], force_refresh=True)
        assert results == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
