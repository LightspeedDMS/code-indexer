"""
Unit tests for detect_semantic_index() in repository_health.py.

Tests that the helper correctly finds semantic indexes from any embedding provider
(not just voyage-code-3) and excludes multimodal/temporal/tantivy collections.
"""

from pathlib import Path

from code_indexer.server.routers.repository_health import detect_semantic_index


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
