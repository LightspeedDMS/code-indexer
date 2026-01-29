"""Integration tests for TextChunker with image validation logging - Story #64 AC7."""

import json
import logging
from io import StringIO
from pathlib import Path
import tempfile
import shutil
from unittest.mock import patch

import pytest

from src.code_indexer.indexing.chunker import TextChunker
from src.code_indexer.config import IndexingConfig


class TestChunkerImageValidationLogging:
    """Test chunker logs skipped images using AdaptiveLogger."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()

        # Create test images
        self.images_dir = self.repo_root / "images"
        self.images_dir.mkdir()
        (self.images_dir / "valid.png").write_bytes(b"fake png")
        (self.images_dir / "valid.jpg").write_bytes(b"fake jpg")

        # Create config
        self.config = IndexingConfig(chunk_size=1000, chunk_overlap=200)
        self.chunker = TextChunker(self.config)

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_chunker_logs_missing_image(self):
        """Test that chunker logs when image is missing."""
        content = """
![Valid](images/valid.png)
![Missing](images/missing.png)
"""
        file_path = self.repo_root / "doc.md"

        # Capture stderr (CLI mode logging)
        output = StringIO()
        with patch('sys.stderr', output):
            # Enable verbose logging
            chunks = self.chunker.chunk_text_with_logging(
                content, file_path, repo_root=self.repo_root, verbose=True
            )

        # Should have 1 valid image
        assert len(chunks) >= 1
        images = chunks[0]["images"]
        assert len(images) == 1
        assert "images/valid.png" in images

        # Should have logged the missing image
        log_output = output.getvalue()
        assert "[WARN]" in log_output
        assert "images/missing.png" in log_output
        assert "missing" in log_output.lower() or "not found" in log_output.lower()

    def test_chunker_logs_unsupported_format(self):
        """Test that chunker logs when image format is unsupported."""
        # Create unsupported BMP file
        (self.images_dir / "unsupported.bmp").write_bytes(b"fake bmp")

        content = """
![Valid](images/valid.png)
![Unsupported](images/unsupported.bmp)
"""
        file_path = self.repo_root / "doc.md"

        output = StringIO()
        with patch('sys.stderr', output):
            chunks = self.chunker.chunk_text_with_logging(
                content, file_path, repo_root=self.repo_root, verbose=True
            )

        # Should have 1 valid image
        images = chunks[0]["images"]
        assert len(images) == 1
        assert "images/valid.png" in images

        # Should have logged the unsupported format
        log_output = output.getvalue()
        assert "[WARN]" in log_output
        assert "images/unsupported.bmp" in log_output
        assert "unsupported" in log_output.lower() or "format" in log_output.lower()

    def test_chunker_logs_oversized_image(self):
        """Test that chunker logs when image exceeds size limit."""
        # Create 15MB file (exceeds 10MB limit)
        (self.images_dir / "huge.png").write_bytes(b"x" * (15 * 1024 * 1024))

        content = """
![Valid](images/valid.png)
![Huge](images/huge.png)
"""
        file_path = self.repo_root / "doc.md"

        output = StringIO()
        with patch('sys.stderr', output):
            chunks = self.chunker.chunk_text_with_logging(
                content, file_path, repo_root=self.repo_root, verbose=True
            )

        # Should have 1 valid image (small one)
        images = chunks[0]["images"]
        assert len(images) == 1
        assert "images/valid.png" in images

        # Should have logged the oversized image
        log_output = output.getvalue()
        assert "[WARN]" in log_output
        assert "images/huge.png" in log_output
        assert "oversized" in log_output.lower() or "size" in log_output.lower()

    def test_chunker_logs_remote_url(self):
        """Test that chunker logs when remote URL is encountered."""
        content = """
![Valid](images/valid.png)
![Remote](https://example.com/image.png)
"""
        file_path = self.repo_root / "doc.md"

        output = StringIO()
        with patch('sys.stderr', output):
            chunks = self.chunker.chunk_text_with_logging(
                content, file_path, repo_root=self.repo_root, verbose=True
            )

        # Should have 1 valid image
        images = chunks[0]["images"]
        assert len(images) == 1
        assert "images/valid.png" in images

        # Should have logged the remote URL
        log_output = output.getvalue()
        assert "[WARN]" in log_output
        assert "https://example.com/image.png" in log_output
        assert "remote" in log_output.lower() or "url" in log_output.lower()

    def test_chunker_no_logging_in_non_verbose_mode(self):
        """Test that chunker doesn't log in non-verbose CLI mode."""
        content = """
![Valid](images/valid.png)
![Missing](images/missing.png)
"""
        file_path = self.repo_root / "doc.md"

        output = StringIO()
        with patch('sys.stderr', output):
            # Non-verbose mode (default)
            chunks = self.chunker.chunk_text_with_logging(
                content, file_path, repo_root=self.repo_root, verbose=False
            )

        # Should still have valid image
        images = chunks[0]["images"]
        assert "images/valid.png" in images

        # Should NOT have logged anything (non-verbose)
        log_output = output.getvalue()
        assert log_output == ""

    def test_chunker_continues_processing_despite_invalid_images(self):
        """Test that chunker never fails - continues processing all images."""
        # Create a mix of valid and invalid scenarios
        (self.images_dir / "invalid.bmp").write_bytes(b"bmp")
        (self.images_dir / "huge.png").write_bytes(b"x" * (15 * 1024 * 1024))

        content = """
![Valid 1](images/valid.png)
![Missing](images/missing.png)
![Invalid](images/invalid.bmp)
![Remote](https://example.com/remote.png)
![Oversized](images/huge.png)
![Valid 2](images/valid.jpg)
"""
        file_path = self.repo_root / "doc.md"

        output = StringIO()
        with patch('sys.stderr', output):
            # Should NOT raise any exceptions
            chunks = self.chunker.chunk_text_with_logging(
                content, file_path, repo_root=self.repo_root, verbose=True
            )

        # Should have 2 valid images
        images = chunks[0]["images"]
        assert len(images) == 2
        assert "images/valid.png" in images
        assert "images/valid.jpg" in images

        # Should have logged all 4 invalid images
        log_output = output.getvalue()
        assert "images/missing.png" in log_output
        assert "images/invalid.bmp" in log_output
        assert "https://example.com/remote.png" in log_output
        assert "images/huge.png" in log_output

    def test_chunker_server_mode_json_logging(self):
        """Test that chunker logs JSON in server mode."""
        content = """
![Valid](images/valid.png)
![Missing](images/missing.png)
"""
        file_path = self.repo_root / "doc.md"

        # Capture Python logging output
        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setFormatter(logging.Formatter('%(message)s'))

        py_logger = logging.getLogger('cidx.image_extractor')
        py_logger.addHandler(handler)
        py_logger.setLevel(logging.WARNING)

        try:
            # Force server mode - need to create chunker INSIDE the patched environment
            # so AdaptiveLogger detects server context correctly
            with patch.dict('os.environ', {'FASTAPI_APP': 'true'}):
                # Create new chunker in server mode
                server_chunker = TextChunker(self.config)
                chunks = server_chunker.chunk_text_with_logging(
                    content, file_path, repo_root=self.repo_root, verbose=False
                )

            # Should have valid image
            images = chunks[0]["images"]
            assert "images/valid.png" in images

            # Should have JSON log output
            log_output = log_capture.getvalue()
            assert log_output.strip()

            # Parse JSON
            log_data = json.loads(log_output.strip())
            assert log_data["level"] == "warning"
            assert log_data["event"] == "image_skipped"
            assert "images/missing.png" in log_data["image_ref"]
        finally:
            py_logger.removeHandler(handler)
