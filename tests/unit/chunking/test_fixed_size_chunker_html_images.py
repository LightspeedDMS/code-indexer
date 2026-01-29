"""Unit tests for FixedSizeChunker HTML/HTMX image extraction - TDD for Story #69.

These tests verify that FixedSizeChunker properly extracts images from HTML and HTMX files
using the ImageExtractorFactory, not just from markdown files.
"""

import pytest
from pathlib import Path
import tempfile
import shutil

from src.code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from src.code_indexer.config import IndexingConfig


class TestFixedSizeChunkerHtmlImages:
    """Test FixedSizeChunker integration with HTML/HTMX image extraction."""

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
        self.chunker = FixedSizeChunker(self.config)

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_chunk_text_html_with_images(self):
        """Test that chunk_text includes images[] for HTML content with images."""
        content = """<!DOCTYPE html>
<html>
<head>
    <title>Garden Tips</title>
</head>
<body>
    <h1>Spring Gardening</h1>

    <p>Here's our soil composition:</p>
    <img src="../images/diagram.png" alt="Soil Diagram">

    <p>And our planting schedule:</p>
    <img src="../images/chart.jpg" alt="Planting Chart">

    <p>Some gardening content here.</p>
</body>
</html>
"""
        file_path = self.repo_root / "docs" / "garden-tips.html"

        # Chunk the text
        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        # Should have at least one chunk
        assert len(chunks) >= 1

        # First chunk should have images[] field
        assert "images" in chunks[0]

        # Images should be extracted from HTML img tags
        images = chunks[0]["images"]
        assert isinstance(images, list)
        # HTML files should extract images using HtmlImageExtractor
        assert len(images) == 2
        # Note: Images are dict format from HtmlImageExtractor
        assert any("diagram.png" in str(img) for img in images)
        assert any("chart.jpg" in str(img) for img in images)

    def test_chunk_text_htmx_with_images(self):
        """Test that chunk_text includes images[] for HTMX content with images."""
        content = """<div hx-get="/api/content">
    <h2>Astronomy Basics</h2>

    <img src="../images/diagram.png" alt="Solar System">

    <p>Learn about the planets and stars.</p>

    <img src="../images/chart.jpg" alt="Constellation Map">
</div>
"""
        file_path = self.repo_root / "partials" / "astronomy-basics.htmx"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]

        images = chunks[0]["images"]
        assert isinstance(images, list)
        assert len(images) == 2
        assert any("diagram.png" in str(img) for img in images)
        assert any("chart.jpg" in str(img) for img in images)

    def test_chunk_text_html_without_images(self):
        """Test that chunk_text includes empty images[] for HTML without images."""
        content = """<!DOCTYPE html>
<html>
<head>
    <title>Text Only</title>
</head>
<body>
    <h1>No Images</h1>
    <p>Just text content, no images.</p>
</body>
</html>
"""
        file_path = self.repo_root / "docs" / "text-only.html"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        assert chunks[0]["images"] == []

    def test_chunk_text_html_filters_remote_urls(self):
        """Test that remote URLs are filtered from HTML images[]."""
        content = """<html>
<body>
    <img src="images/diagram.png" alt="Local">
    <img src="https://example.com/image.png" alt="Remote">
</body>
</html>
"""
        file_path = self.repo_root / "doc.html"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        images = chunks[0]["images"]
        # Only local image should be extracted
        assert len(images) == 1
        assert any("diagram.png" in str(img) for img in images)
        assert not any("http" in str(img) for img in images)

    def test_chunk_text_html_filters_data_uris(self):
        """Test that data URIs are filtered from HTML images[]."""
        content = """<html>
<body>
    <img src="images/diagram.png" alt="File">
    <img src="data:image/png;base64,iVBORw0KGgo=" alt="Data URI">
</body>
</html>
"""
        file_path = self.repo_root / "doc.html"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        images = chunks[0]["images"]
        # Only file-based image should be extracted (not data URI)
        assert len(images) == 1
        assert any("diagram.png" in str(img) for img in images)
        assert not any("data:" in str(img) for img in images)

    def test_chunk_file_html_with_images(self):
        """Test chunk_file() method with HTML file containing images."""
        # Create HTML file
        html_file = self.repo_root / "docs" / "guide.html"
        html_file.parent.mkdir(parents=True, exist_ok=True)

        content = """<!DOCTYPE html>
<html>
<head><title>Guide</title></head>
<body>
    <h1>Guide</h1>
    <img src="../images/diagram.png" alt="Diagram">
</body>
</html>
"""
        html_file.write_text(content)

        # Chunk the file
        chunks = self.chunker.chunk_file(html_file, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        assert len(chunks[0]["images"]) == 1
        assert any("diagram.png" in str(img) for img in chunks[0]["images"])

    def test_chunk_file_htmx_with_images(self):
        """Test chunk_file() method with HTMX file containing images."""
        # Create HTMX file
        htmx_file = self.repo_root / "partials" / "content.htmx"
        htmx_file.parent.mkdir(parents=True, exist_ok=True)

        content = """<div>
    <img src="../images/chart.jpg" alt="Chart">
    <p>Dynamic content</p>
</div>
"""
        htmx_file.write_text(content)

        chunks = self.chunker.chunk_file(htmx_file, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        assert len(chunks[0]["images"]) == 1
        assert any("chart.jpg" in str(img) for img in chunks[0]["images"])

    def test_chunk_text_markdown_still_works(self):
        """Test that markdown image extraction still works (regression check)."""
        content = """# Documentation

![Diagram](../images/diagram.png)

![Chart](../images/chart.jpg)

Some text content here.
"""
        file_path = self.repo_root / "docs" / "guide.md"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        images = chunks[0]["images"]
        assert len(images) == 2
        assert any("diagram.png" in str(img) for img in images)
        assert any("chart.jpg" in str(img) for img in images)

    def test_chunk_text_non_supported_extension_no_images(self):
        """Test that unsupported file types have empty images[]."""
        content = """
def hello():
    # Comment with ![fake](image.png)
    print("Hello world")
"""
        file_path = self.repo_root / "src" / "main.py"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        # Python files should NOT extract images (not supported)
        assert chunks[0]["images"] == []

    def test_chunk_text_htm_extension_supported(self):
        """Test that .htm extension is also supported (not just .html)."""
        content = """<html>
<body>
    <img src="images/diagram.png" alt="Test">
</body>
</html>
"""
        file_path = self.repo_root / "page.htm"

        chunks = self.chunker.chunk_text(content, file_path, repo_root=self.repo_root)

        assert len(chunks) >= 1
        assert "images" in chunks[0]
        images = chunks[0]["images"]
        assert len(images) == 1
        assert any("diagram.png" in str(img) for img in images)
