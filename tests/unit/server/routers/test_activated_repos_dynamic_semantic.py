"""
Unit tests for resolve_semantic_index_path() in activated_repos.py.

Tests that the helper correctly resolves the semantic index path from any
embedding provider (not just voyage-code-3) and uses voyage-code-3 as
an explicit fallback when no other candidate is found.
"""

from pathlib import Path

from code_indexer.server.routers.activated_repos import resolve_semantic_index_path


def _create_collection(index_dir: Path, name: str, with_hnsw: bool = True) -> Path:
    """Create a fake collection directory, optionally with hnsw_index.bin."""
    coll = index_dir / name
    coll.mkdir(parents=True, exist_ok=True)
    if with_hnsw:
        (coll / "hnsw_index.bin").write_bytes(b"")
    return coll


class TestResolveSemanticIndexPath:
    """Tests for resolve_semantic_index_path() helper."""

    def test_returns_voyage_code_3_fallback_when_no_index_dir(self, tmp_path):
        """Returns voyage-code-3 fallback path when index directory does not exist."""
        index_dir = tmp_path / ".code-indexer" / "index"

        result = resolve_semantic_index_path(index_dir)

        assert result == index_dir / "voyage-code-3" / "hnsw_index.bin"

    def test_returns_voyage_code_3_fallback_when_no_other_candidate(self, tmp_path):
        """Returns voyage-code-3 fallback when it is the only collection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "voyage-code-3")

        result = resolve_semantic_index_path(index_dir)

        assert result == index_dir / "voyage-code-3" / "hnsw_index.bin"

    def test_detects_embed_v4_collection(self, tmp_path):
        """Resolves embed-v4.0/hnsw_index.bin as semantic index path."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0")

        result = resolve_semantic_index_path(index_dir)

        assert result == index_dir / "embed-v4.0" / "hnsw_index.bin"

    def test_excludes_multimodal_collection(self, tmp_path):
        """Excludes embed-v4.0-multimodal from semantic path resolution."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0-multimodal")

        result = resolve_semantic_index_path(index_dir)

        # Should fall back to voyage-code-3 since only multimodal exists
        assert result == index_dir / "voyage-code-3" / "hnsw_index.bin"

    def test_excludes_temporal_collection(self, tmp_path):
        """Excludes temporal collections from semantic path resolution."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "code-indexer-temporal")

        result = resolve_semantic_index_path(index_dir)

        assert result == index_dir / "voyage-code-3" / "hnsw_index.bin"

    def test_excludes_tantivy_collection(self, tmp_path):
        """Excludes tantivy (and tantivy-variant names) from semantic path resolution."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "tantivy")
        _create_collection(index_dir, "tantivy-v2")

        result = resolve_semantic_index_path(index_dir)

        assert result == index_dir / "voyage-code-3" / "hnsw_index.bin"

    def test_prefers_non_voyage_provider_over_fallback(self, tmp_path):
        """Prefers embed-v4.0 over voyage-code-3 fallback when both exist."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0")
        _create_collection(index_dir, "voyage-code-3")

        result = resolve_semantic_index_path(index_dir)

        assert result == index_dir / "embed-v4.0" / "hnsw_index.bin"

    def test_resolves_semantic_among_mixed_collections(self, tmp_path):
        """Resolves semantic path even when multimodal and temporal also exist."""
        index_dir = tmp_path / ".code-indexer" / "index"

        # Add excluded collections
        for excluded in ["voyage-multimodal-3", "code-indexer-temporal", "tantivy"]:
            _create_collection(index_dir, excluded)

        # Add a valid semantic collection
        _create_collection(index_dir, "embed-v4.0")

        result = resolve_semantic_index_path(index_dir)

        assert result == index_dir / "embed-v4.0" / "hnsw_index.bin"
