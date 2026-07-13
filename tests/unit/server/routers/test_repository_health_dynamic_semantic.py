"""
Unit tests for detect_semantic_index() in repository_health.py.

Tests that the helper correctly finds semantic indexes from any embedding provider
(not just voyage-code-3) and excludes multimodal/temporal/tantivy collections.
"""

from pathlib import Path

from code_indexer.server.routers.repository_health import (
    build_incomplete_collection_result,
    collection_has_vector_shards,
    detect_semantic_index,
    _classify_index_type,
)


def _create_collection(index_dir: Path, name: str, with_hnsw: bool = True) -> Path:
    """Create a fake collection directory, optionally with hnsw_index.bin."""
    coll = index_dir / name
    coll.mkdir(parents=True, exist_ok=True)
    if with_hnsw:
        (coll / "hnsw_index.bin").write_bytes(b"")
    return coll


class TestDetectSemanticIndex:
    """Tests for detect_semantic_index() helper."""

    def test_detects_voyage_code_3_collection(self, tmp_path):
        """Detects voyage-code-3 hnsw_index.bin as semantic index."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "voyage-code-3")

        assert detect_semantic_index(index_dir) is True

    def test_detects_embed_v4_collection(self, tmp_path):
        """Detects embed-v4.0 hnsw_index.bin as semantic index (Cohere provider)."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0")

        assert detect_semantic_index(index_dir) is True

    def test_excludes_embed_v4_multimodal_collection(self, tmp_path):
        """Excludes embed-v4.0-multimodal from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0-multimodal")

        assert detect_semantic_index(index_dir) is False

    def test_excludes_voyage_multimodal_collection(self, tmp_path):
        """Excludes voyage-multimodal-3 from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "voyage-multimodal-3")

        assert detect_semantic_index(index_dir) is False

    def test_excludes_temporal_collection(self, tmp_path):
        """Excludes temporal collections from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "code-indexer-temporal")

        assert detect_semantic_index(index_dir) is False

    def test_excludes_tantivy_collection(self, tmp_path):
        """Excludes tantivy (and tantivy-v2 style names) from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "tantivy")
        _create_collection(index_dir, "tantivy-v2")

        assert detect_semantic_index(index_dir) is False

    def test_returns_false_when_no_hnsw_bin_present(self, tmp_path):
        """Returns False when collection dir exists but has no hnsw_index.bin."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0", with_hnsw=False)

        assert detect_semantic_index(index_dir) is False

    def test_returns_false_when_index_dir_missing(self, tmp_path):
        """Returns False when .code-indexer/index doesn't exist."""
        missing_dir = tmp_path / ".code-indexer" / "index"

        assert detect_semantic_index(missing_dir) is False

    def test_detects_semantic_among_mixed_collections(self, tmp_path):
        """Detects semantic index even when multimodal and temporal also exist."""
        index_dir = tmp_path / ".code-indexer" / "index"

        # Add excluded collections
        for excluded in ["voyage-multimodal-3", "code-indexer-temporal", "tantivy"]:
            _create_collection(index_dir, excluded)

        # Add a valid semantic collection
        _create_collection(index_dir, "embed-v4.0")

        assert detect_semantic_index(index_dir) is True


def _create_shard(collection_dir: Path, name: str = "vector_0001.json") -> Path:
    """Write a vector shard file inside a collection directory."""
    collection_dir.mkdir(parents=True, exist_ok=True)
    shard = collection_dir / name
    shard.write_text('{"id": "1", "vector": [0.0]}')
    return shard


class TestClassifyIndexType:
    """Tests for _classify_index_type() helper."""

    def test_semantic_default(self):
        assert _classify_index_type("voyage-code-3") == "semantic"
        assert _classify_index_type("embed-v4.0") == "semantic"

    def test_temporal(self):
        assert _classify_index_type("code-indexer-temporal") == "temporal"

    def test_multimodal(self):
        assert _classify_index_type("voyage-multimodal-3") == "multimodal"


class TestCollectionHasVectorShards:
    """Tests for collection_has_vector_shards() helper."""

    def test_true_with_top_level_shard(self, tmp_path):
        coll = tmp_path / "voyage-code-3"
        _create_shard(coll)

        assert collection_has_vector_shards(coll) is True

    def test_true_with_nested_shard(self, tmp_path):
        """Shards can live in subdirectories — rglob must find them."""
        coll = tmp_path / "voyage-code-3"
        nested = coll / "shards" / "aa"
        _create_shard(nested)

        assert collection_has_vector_shards(coll) is True

    def test_false_for_empty_collection(self, tmp_path):
        coll = tmp_path / "voyage-code-3"
        coll.mkdir(parents=True)

        assert collection_has_vector_shards(coll) is False


class TestIncompleteCollectionScan:
    """EVO-64245: a populated collection missing hnsw_index.bin must surface
    as unhealthy instead of being silently skipped by the health scan."""

    def _scan_decision(self, collection_dir: Path):
        """Reproduce the get_repository_health() per-collection decision for a
        collection whose hnsw_index.bin is checked below.

        Returns a CollectionHealthResult when the collection would be recorded,
        or None when the scan would skip it (mirrors the router loop).
        """
        hnsw_file = collection_dir / "hnsw_index.bin"
        if not hnsw_file.exists():
            if collection_has_vector_shards(collection_dir):
                return build_incomplete_collection_result(collection_dir)
            return None
        # hnsw present -> a normal health check would run (not "incomplete")
        return "would-health-check"

    def test_shards_without_hnsw_reported_unhealthy(self, tmp_path):
        """Shards + collection_meta.json but no hnsw_index.bin -> unhealthy."""
        coll = tmp_path / "voyage-code-3"
        _create_shard(coll)
        (coll / "collection_meta.json").write_text('{"vector_dim": 1536}')

        result = self._scan_decision(coll)

        assert result is not None
        assert result != "would-health-check"
        assert result.valid is False
        assert result.file_exists is False
        assert result.loadable is False
        assert result.collection_name == "voyage-code-3"
        assert result.index_type == "semantic"
        assert result.errors
        assert "HNSW graph missing" in result.errors[0]

    def test_empty_collection_not_flagged(self, tmp_path):
        """Genuinely empty (never-indexed) collection -> skipped, no alarm."""
        coll = tmp_path / "voyage-code-3"
        coll.mkdir(parents=True)

        assert self._scan_decision(coll) is None

    def test_meta_only_zero_vectors_not_flagged(self, tmp_path):
        """A collection with only a (0-vector) collection_meta.json and no
        shards is legitimately empty and must not be false-flagged."""
        coll = tmp_path / "voyage-code-3"
        coll.mkdir(parents=True)
        (coll / "collection_meta.json").write_text('{"vector_dim": 1536}')

        assert self._scan_decision(coll) is None

    def test_healthy_collection_not_incomplete(self, tmp_path):
        """A collection with hnsw_index.bin present is not treated as
        incomplete (it proceeds to the normal health check path)."""
        coll = tmp_path / "voyage-code-3"
        _create_shard(coll)
        (coll / "hnsw_index.bin").write_bytes(b"")

        assert self._scan_decision(coll) == "would-health-check"

    def test_incomplete_result_classifies_temporal(self, tmp_path):
        """Index type is classified from the collection name."""
        coll = tmp_path / "code-indexer-temporal"
        _create_shard(coll)

        result = build_incomplete_collection_result(coll)

        assert result.index_type == "temporal"
        assert result.valid is False
