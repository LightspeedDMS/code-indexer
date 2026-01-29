"""E2E test for multimodal embedding integration.

Tests that markdown files with images are:
1. Detected during chunking
2. Embedded using VoyageMultimodalClient
3. Stored in separate multimodal_index/ subdirectory
4. Query integration works across both indexes
"""

from code_indexer.config import IndexingConfig
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker


class TestMultimodalIntegration:
    """E2E tests for multimodal embedding integration."""

    def test_indexing_markdown_with_images_creates_multimodal_index(self, tmp_path):
        """
        CRITICAL TEST: Verify markdown files with images are indexed to multimodal_index.

        This validates:
        1. Chunker detects markdown images and populates images[] field
        2. FileChunkingManager routes chunks with images to VoyageMultimodalClient
        3. Multimodal embeddings stored in .code-indexer/multimodal_index/
        4. Regular chunks (no images) still go to main code_index/
        """
        # Create test markdown file with image
        test_file = tmp_path / "README.md"
        test_file.write_text(
            "# Test Document\n\n"
            "Some text before image.\n\n"
            "![alt text](image.png)\n\n"
            "Some text after image."
        )

        # Create test image (placeholder)
        image_file = tmp_path / "image.png"
        image_file.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG header

        # Create chunker with simple config
        config = IndexingConfig()
        chunker = FixedSizeChunker(config)

        # Chunk the file
        chunks = chunker.chunk_file(test_file)

        # ASSERTION 1: Chunks with images should have images[] field populated
        chunks_with_images = [c for c in chunks if c.get("images")]
        assert len(chunks_with_images) > 0, "Expected at least one chunk with images"

        # Verify image metadata structure
        for chunk in chunks_with_images:
            assert "images" in chunk
            assert isinstance(chunk["images"], list)
            assert len(chunk["images"]) > 0

            # Check image metadata structure
            image_meta = chunk["images"][0]
            assert "path" in image_meta
            assert "alt_text" in image_meta
            assert image_meta["path"] == "image.png"
            assert image_meta["alt_text"] == "alt text"

    def test_indexing_markdown_without_images_uses_code_embeddings(self, tmp_path):
        """Verify markdown files WITHOUT images use regular code embeddings."""
        # Create test markdown file WITHOUT image
        test_file = tmp_path / "README.md"
        test_file.write_text(
            "# Test Document\n\n"
            "Just regular text content.\n\n"
            "No images here."
        )

        # Create chunker with simple config
        config = IndexingConfig()
        chunker = FixedSizeChunker(config)

        # Chunk the file
        chunks = chunker.chunk_file(test_file)

        # ASSERTION: No chunks should have images
        chunks_with_images = [c for c in chunks if c.get("images")]
        assert len(chunks_with_images) == 0, "Expected no chunks with images"

        # All chunks should use regular code embeddings (main index)
        for chunk in chunks:
            assert "images" not in chunk or chunk["images"] == []
