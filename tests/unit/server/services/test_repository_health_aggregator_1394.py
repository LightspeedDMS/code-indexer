"""
Bug #1394: shared discovery + response-shaping helper.

Consolidates logic that was duplicated nearly verbatim between
repository_health.py::get_repository_health and activated_repos.py::get_health
into one module: CollectionHealthResult / RepositoryHealthResult models,
_to_collection_health_result mapping, discover_health_collections (the
per-collection-directory scan), get_shared_health_service (one 5-minute-TTL
HNSWHealthService singleton reused by BOTH routers instead of each router
building its own fresh, cache-less instance), and compute_repository_health
(the aggregation entry point that both routers' GET handlers and the new
async job workers all call).

Real on-disk hnswlib indexes -- no mocking of HNSWHealthService/check_health.
"""

from __future__ import annotations

from pathlib import Path

import hnswlib
import numpy as np
import pytest

from code_indexer.server.services.repository_health_aggregator import (
    CollectionHealthResult,
    RepositoryHealthResult,
    _to_collection_health_result,
    compute_repository_health,
    discover_health_collections,
    get_shared_health_service,
)
from code_indexer.services.hnsw_health_service import (
    HealthCheckResult,
    HNSWHealthService,
)

DIM = 16


def _build_real_index(path: Path, num_elements: int = 20) -> None:
    """Build and save a small, genuinely valid on-disk HNSW index."""
    rng = np.random.RandomState(3)
    vectors = rng.randn(num_elements, DIM).astype(np.float32)

    index = hnswlib.Index(space="l2", dim=DIM)
    index.init_index(max_elements=num_elements, ef_construction=100, M=8)
    index.add_items(vectors, np.arange(num_elements))
    index.save_index(str(path))


def _make_health_result(valid: bool) -> HealthCheckResult:
    return HealthCheckResult(  # type: ignore[call-arg]
        valid=valid,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=100,
        connections_checked=500,
        min_inbound=0 if not valid else 2,
        max_inbound=10,
        orphan_count=0 if valid else 3,
        index_path="/fake/hnsw_index.bin",
        file_size_bytes=1024,
        errors=[] if valid else ["orphan detected"],
        check_duration_ms=1.0,
    )


class TestToCollectionHealthResult:
    def test_maps_health_result_onto_collection_health_result(self):
        health_result = _make_health_result(valid=True)
        collection = _to_collection_health_result(
            "voyage-code-3", "semantic", health_result
        )
        assert isinstance(collection, CollectionHealthResult)
        assert collection.collection_name == "voyage-code-3"
        assert collection.index_type == "semantic"
        assert collection.valid is True
        assert collection.orphan_count == 0


class TestDiscoverHealthCollections:
    def test_discovers_semantic_temporal_and_multimodal(self, tmp_path: Path):
        index_base = tmp_path / "index"
        for name in ["voyage-code-3", "code-indexer-temporal", "voyage-multimodal-3"]:
            coll = index_base / name
            coll.mkdir(parents=True)
            (coll / "hnsw_index.bin").write_bytes(b"")

        discovered = discover_health_collections(index_base)
        by_name = {name: index_type for name, index_type, _path in discovered}

        assert by_name["voyage-code-3"] == "semantic"
        assert by_name["code-indexer-temporal"] == "temporal"
        assert by_name["voyage-multimodal-3"] == "multimodal"

    def test_skips_directories_without_hnsw_index_bin(self, tmp_path: Path):
        index_base = tmp_path / "index"
        empty_coll = index_base / "no-index-here"
        empty_coll.mkdir(parents=True)

        discovered = discover_health_collections(index_base)
        assert discovered == []

    def test_returns_correct_path_per_collection(self, tmp_path: Path):
        index_base = tmp_path / "index"
        coll = index_base / "embed-v4.0"
        coll.mkdir(parents=True)
        hnsw_file = coll / "hnsw_index.bin"
        hnsw_file.write_bytes(b"")

        discovered = discover_health_collections(index_base)
        assert len(discovered) == 1
        name, index_type, path = discovered[0]
        assert name == "embed-v4.0"
        assert index_type == "semantic"
        assert path == hnsw_file

    def test_deterministic_ordering(self, tmp_path: Path):
        index_base = tmp_path / "index"
        for name in ["zzz-collection", "aaa-collection", "mmm-collection"]:
            coll = index_base / name
            coll.mkdir(parents=True)
            (coll / "hnsw_index.bin").write_bytes(b"")

        discovered_1 = discover_health_collections(index_base)
        discovered_2 = discover_health_collections(index_base)
        assert discovered_1 == discovered_2
        names_in_order = [name for name, _type, _path in discovered_1]
        assert names_in_order == sorted(names_in_order)


class TestGetSharedHealthService:
    def test_returns_same_singleton_instance_across_calls(self):
        service_1 = get_shared_health_service()
        service_2 = get_shared_health_service()
        assert service_1 is service_2

    def test_singleton_is_hnsw_health_service_with_300s_ttl(self):
        service = get_shared_health_service()
        assert isinstance(service, HNSWHealthService)
        assert service._cache_ttl == 300


class TestComputeRepositoryHealthEmptyCases:
    def test_missing_index_base_path_returns_empty_healthy_result(self, tmp_path: Path):
        index_base = tmp_path / "does-not-exist"
        service = HNSWHealthService(cache_ttl_seconds=300)

        result = compute_repository_health("my-repo", index_base, service)

        assert isinstance(result, RepositoryHealthResult)
        assert result.repo_alias == "my-repo"
        assert result.overall_healthy is True
        assert result.collections == []
        assert result.total_collections == 0
        assert result.healthy_count == 0
        assert result.unhealthy_count == 0
        assert result.from_cache is False

    def test_index_base_path_is_a_file_not_dir_returns_empty_result(
        self, tmp_path: Path
    ):
        index_base = tmp_path / "index_as_file"
        index_base.write_bytes(b"not a directory")
        service = HNSWHealthService(cache_ttl_seconds=300)

        result = compute_repository_health("my-repo", index_base, service)

        assert result.overall_healthy is True
        assert result.total_collections == 0

    def test_empty_index_dir_no_collections_returns_empty_result(self, tmp_path: Path):
        index_base = tmp_path / "index"
        index_base.mkdir(parents=True)
        service = HNSWHealthService(cache_ttl_seconds=300)

        result = compute_repository_health("my-repo", index_base, service)

        assert result.overall_healthy is True
        assert result.total_collections == 0


class TestComputeRepositoryHealthAggregation:
    def test_all_healthy_collections_aggregate_correctly(self, tmp_path: Path):
        index_base = tmp_path / "index"
        for name in ["voyage-code-3", "code-indexer-temporal"]:
            coll = index_base / name
            coll.mkdir(parents=True)
            _build_real_index(coll / "hnsw_index.bin")

        service = HNSWHealthService(cache_ttl_seconds=300)
        result = compute_repository_health(
            "my-repo", index_base, service, force_refresh=True
        )

        assert result.repo_alias == "my-repo"
        assert result.total_collections == 2
        assert result.healthy_count == 2
        assert result.unhealthy_count == 0
        assert result.overall_healthy is True
        assert {c.collection_name for c in result.collections} == {
            "voyage-code-3",
            "code-indexer-temporal",
        }
        for c in result.collections:
            assert c.valid is True

    def test_one_unhealthy_collection_flips_overall_healthy_false(self, tmp_path: Path):
        index_base = tmp_path / "index"
        healthy_coll = index_base / "voyage-code-3"
        healthy_coll.mkdir(parents=True)
        _build_real_index(healthy_coll / "hnsw_index.bin")

        corrupt_coll = index_base / "embed-v4.0"
        corrupt_coll.mkdir(parents=True)
        (corrupt_coll / "hnsw_index.bin").write_bytes(b"not a real index")

        service = HNSWHealthService(cache_ttl_seconds=300)
        result = compute_repository_health(
            "my-repo", index_base, service, force_refresh=True
        )

        assert result.total_collections == 2
        assert result.healthy_count == 1
        assert result.unhealthy_count == 1
        assert result.overall_healthy is False

        by_name = {c.collection_name: c for c in result.collections}
        assert by_name["voyage-code-3"].valid is True
        assert by_name["embed-v4.0"].valid is False

    def test_passes_max_workers_through_to_batch_check(self, tmp_path: Path):
        """compute_repository_health must not swallow max_workers silently."""
        index_base = tmp_path / "index"
        for i in range(3):
            coll = index_base / f"coll-{i}"
            coll.mkdir(parents=True)
            _build_real_index(coll / "hnsw_index.bin")

        service = HNSWHealthService(cache_ttl_seconds=300)
        # Should not raise even with a constrained worker count.
        result = compute_repository_health(
            "my-repo", index_base, service, force_refresh=True, max_workers=1
        )
        assert result.total_collections == 3
        assert result.healthy_count == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
