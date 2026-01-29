"""Unit tests for TextChunker images[] integration - TDD Story #62 AC1."""

import pytest
from pathlib import Path
import tempfile
import shutil

from src.code_indexer.indexing.chunker import TextChunker
from src.code_indexer.config import IndexingConfig


class TestTextChunkerImagesIntegration:
    """Test TextChunker integration with markdown image extraction."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()

        # Create test images
        self.images_dir = self.repo_root / "images"
        self.images_dir.mkdir()
        (self.images_dir / "diagram.png").write_bytes(b"fake png")
        (self.images_dir / "chart.jpg").write_bytes(b"fake jpg")

        # Create config
        self.config = IndexingConfig(chunk_size=1000, chunk_overlap=200)
        self.chunker = TextChunker(self.config)

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_chunk_text_markdown_with_images(self):
        """Test that chunk_text includes images[] for markdown content with images."""
        content = """# Documentation

Here's a diagram:

![Diagram](../images/diagram.png)

And a chart:

![Chart](../images/chart.jpg)

Some text content here.
"""
        file_path = self.repo_root / "docs" / "guide.md"

        # Chunk the text (passing repo_root for image extraction)
        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        # Should have at least one chunk
        assert len(chunks) >= 1

        # First chunk should have images[] field
        assert "images" in chunks[0]

        # Images should be extracted and validated
        images = chunks[0]["images"]
        assert isinstance(images, list)
        assert len(images) == 2
        assert "images/diagram.png" in images
        assert "images/chart.jpg" in images

    def test_chunk_text_markdown_without_images(self):
        """Test that chunk_text includes empty images[] for markdown without images."""
        content = """# Documentation

Just text content, no images.
"""
        file_path = self.repo_root / "docs" / "guide.md"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        assert chunks[0]["images"] == []

    def test_chunk_text_non_markdown_no_images(self):
        """Test that chunk_text includes empty images[] for non-markdown files."""
        content = """
def hello():
    print("Hello world")
"""
        file_path = self.repo_root / "src" / "main.py"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        assert chunks[0]["images"] == []

    def test_chunk_text_markdown_filters_remote_urls(self):
        """Test that remote URLs are filtered from images[]."""
        content = """
![Local](images/diagram.png)
![Remote](https://example.com/image.png)
"""
        file_path = self.repo_root / "doc.md"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        images = chunks[0]["images"]
        assert len(images) == 1
        assert "images/diagram.png" in images
        assert not any("http" in img for img in images)

    def test_chunk_text_markdown_validates_images(self):
        """Test that only valid images are included (exist, supported format)."""
        content = """
![Valid](images/diagram.png)
![Invalid format](images/unsupported.bmp)
![Missing](images/missing.png)
"""
        file_path = self.repo_root / "doc.md"

        # Create BMP file (unsupported)
        (self.images_dir / "unsupported.bmp").write_bytes(b"fake bmp")

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        # After validation, only valid images should be included (AC1 requirement)
        images = chunks[0]["images"]
        assert len(images) == 1  # Only the valid PNG
        assert "images/diagram.png" in images
        # Invalid BMP and missing PNG should be filtered out
        assert "images/unsupported.bmp" not in images
        assert "images/missing.png" not in images

    def test_chunk_text_multiple_chunks_distributes_images(self):
        """Test that images appear in the chunks that contain their markdown references."""
        # Create content that will span multiple chunks
        long_text = "Text content.\n" * 50  # ~700 chars

        content = f"""# Part 1

{long_text}

![First](images/diagram.png)

{long_text}

# Part 2

{long_text}

![Second](images/chart.jpg)

{long_text}
"""
        file_path = self.repo_root / "long.md"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        # Should have multiple chunks
        assert len(chunks) > 1

        # Images should be distributed across chunks based on content
        # Each chunk should have its own images[] field
        for chunk in chunks:
            assert "images" in chunk
            assert isinstance(chunk["images"], list)

    def test_chunk_file_markdown_with_images(self):
        """Test chunk_file() method with markdown file containing images."""
        # Create markdown file
        md_file = self.repo_root / "docs" / "guide.md"
        md_file.parent.mkdir(parents=True, exist_ok=True)

        content = """# Guide

![Diagram](../images/diagram.png)
"""
        md_file.write_text(content)

        # Chunk the file
        chunks = self.chunker.chunk_file(md_file, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        assert len(chunks[0]["images"]) == 1
        assert "images/diagram.png" in chunks[0]["images"]

    def test_chunk_text_without_repo_root_no_images(self):
        """Test that without repo_root parameter, images[] is empty (backward compat)."""
        content = """
![Image](images/diagram.png)
"""
        file_path = self.repo_root / "doc.md"

        # Call without repo_root parameter (backward compatibility)
        chunks = self.chunker.chunk_text(content, file_path)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        # Without repo_root, can't extract images properly, so should be empty
        assert chunks[0]["images"] == []

    def test_chunk_text_markdown_html_format(self):
        """Test that HTML img tags are NOT extracted (markdown only for now)."""
        content = """
<img src="images/diagram.png" alt="Diagram" />

![Markdown](images/chart.jpg)
"""
        file_path = self.repo_root / "doc.md"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        images = chunks[0]["images"]
        # Only markdown syntax should be extracted
        assert len(images) == 1
        assert "images/chart.jpg" in images
        # HTML img tag should NOT be extracted
        assert not any("diagram.png" in img for img in images)
