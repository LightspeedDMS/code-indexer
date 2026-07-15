"""
EVO-64245: a partially-built collection must be reported unhealthy.

Indexing writes ``vector_*.json`` shards incrementally and only renames
``hnsw_index.bin`` into place at the very end. If it is interrupted in between
(OOM/crash/timeout), the collection is left populated but permanently
unqueryable. discover_health_collections() cannot see such a collection -- it
has no graph to check -- so the repository previously reported
healthy-with-zero-collections: a false green over a broken index.

A collection directory with neither shards nor a graph is genuinely empty /
never indexed and must still be skipped, so the fix does not false-alarm.

Real on-disk hnswlib indexes and real shard files -- no mocking.
"""

from __future__ import annotations

from pathlib import Path

import hnswlib
import numpy as np

from code_indexer.server.services.repository_health_aggregator import (
    build_incomplete_collection_result,
    collection_has_vector_shards,
    compute_repository_health,
    discover_health_collections,
    discover_incomplete_collections,
    get_shared_health_service,
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


def _write_shard(collection_dir: Path, *, nested: bool = True) -> Path:
    """Write one vector shard, nested as real indexing lays them out."""
    shard_dir = collection_dir / "99" / "66" if nested else collection_dir
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard = shard_dir / "vector_b4f6.json"
    shard.write_text('{"id": "1", "vector": [0.1, 0.2]}')
    return shard


class TestCollectionHasVectorShards:
    def test_finds_shard_nested_in_subdirectories(self, tmp_path: Path):
        collection = tmp_path / "voyage-code-3"
        collection.mkdir()
        _write_shard(collection, nested=True)

        assert collection_has_vector_shards(collection) is True

    def test_finds_shard_at_top_level(self, tmp_path: Path):
        collection = tmp_path / "voyage-code-3"
        collection.mkdir()
        _write_shard(collection, nested=False)

        assert collection_has_vector_shards(collection) is True

    def test_empty_collection_has_no_shards(self, tmp_path: Path):
        collection = tmp_path / "voyage-code-3"
        collection.mkdir()

        assert collection_has_vector_shards(collection) is False


class TestDiscoverIncompleteCollections:
    def test_shards_without_graph_are_incomplete(self, tmp_path: Path):
        index_base = tmp_path / "index"
        collection = index_base / "voyage-code-3"
        collection.mkdir(parents=True)
        _write_shard(collection)

        assert discover_incomplete_collections(index_base) == [collection]
        # The graph-based scan cannot see it -- that is the whole problem.
        assert discover_health_collections(index_base) == []

    def test_complete_collection_is_not_incomplete(self, tmp_path: Path):
        index_base = tmp_path / "index"
        collection = index_base / "voyage-code-3"
        collection.mkdir(parents=True)
        _write_shard(collection)
        _build_real_index(collection / "hnsw_index.bin")

        assert discover_incomplete_collections(index_base) == []

    def test_empty_collection_is_not_incomplete(self, tmp_path: Path):
        index_base = tmp_path / "index"
        (index_base / "voyage-code-3").mkdir(parents=True)

        assert discover_incomplete_collections(index_base) == []

    def test_missing_index_dir_returns_empty(self, tmp_path: Path):
        assert discover_incomplete_collections(tmp_path / "nope") == []


class TestBuildIncompleteCollectionResult:
    def test_result_is_unhealthy_and_names_the_rebuild(self, tmp_path: Path):
        collection = tmp_path / "code-indexer-temporal"
        collection.mkdir()

        result = build_incomplete_collection_result(collection)

        assert result.collection_name == "code-indexer-temporal"
        assert result.index_type == "temporal"
        assert result.valid is False
        assert result.file_exists is False
        assert result.readable is False
        assert result.loadable is False
        assert any("HNSW graph missing" in err for err in result.errors)
        assert any("--rebuild-index" in err for err in result.errors)


class TestComputeRepositoryHealthWithIncompleteCollection:
    def test_partial_collection_makes_repository_unhealthy(self, tmp_path: Path):
        index_base = tmp_path / "index"
        collection = index_base / "voyage-code-3"
        collection.mkdir(parents=True)
        _write_shard(collection)

        result = compute_repository_health(
            "backend", index_base, get_shared_health_service()
        )

        assert result.overall_healthy is False
        assert result.total_collections == 1
        assert result.healthy_count == 0
        assert result.unhealthy_count == 1
        assert result.collections[0].collection_name == "voyage-code-3"
        assert result.collections[0].valid is False
        assert any("HNSW graph missing" in err for err in result.collections[0].errors)

    def test_healthy_and_partial_collections_reported_together(self, tmp_path: Path):
        index_base = tmp_path / "index"

        healthy = index_base / "aaa-healthy"
        healthy.mkdir(parents=True)
        _build_real_index(healthy / "hnsw_index.bin")

        partial = index_base / "zzz-partial"
        partial.mkdir(parents=True)
        _write_shard(partial)

        result = compute_repository_health(
            "backend", index_base, get_shared_health_service(), force_refresh=True
        )

        assert result.total_collections == 2
        assert result.healthy_count == 1
        assert result.unhealthy_count == 1
        assert result.overall_healthy is False
        # Deterministic ordering is preserved across both discovery sources.
        assert [c.collection_name for c in result.collections] == [
            "aaa-healthy",
            "zzz-partial",
        ]

    def test_empty_collection_still_reports_healthy(self, tmp_path: Path):
        index_base = tmp_path / "index"
        (index_base / "voyage-code-3").mkdir(parents=True)

        result = compute_repository_health(
            "backend", index_base, get_shared_health_service()
        )

        assert result.overall_healthy is True
        assert result.total_collections == 0
        assert result.collections == []
