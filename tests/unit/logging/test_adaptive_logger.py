"""Unit tests for AdaptiveLogger - Story #64 AC5-AC6."""

import json
import logging
from io import StringIO
from unittest.mock import patch, MagicMock

import pytest

from src.code_indexer.logging.adaptive_logger import AdaptiveLogger


class TestAdaptiveLoggerContextDetection:
    """Test context detection logic."""

    def test_detect_cli_context_by_default(self):
        """Test that CLI context is detected by default."""
        logger = AdaptiveLogger()
        assert logger._context == "cli"

    @patch('sys.argv', ['uvicorn', 'code_indexer.server.app:app'])
    def test_detect_server_context_with_uvicorn(self):
        """Test that server context is detected when running under uvicorn."""
        logger = AdaptiveLogger()
        assert logger._context == "server"

    @patch.dict('os.environ', {'FASTAPI_APP': 'true'})
    def test_detect_server_context_with_env_var(self):
        """Test that server context is detected via environment variable."""
        logger = AdaptiveLogger()
        assert logger._context == "server"


class TestAdaptiveLoggerCLIMode:
    """Test logging in CLI mode."""

    def setup_method(self):
        """Set up test fixtures."""
        self.logger = AdaptiveLogger()
        self.logger._context = "cli"  # Force CLI mode

    def test_warn_image_skipped_cli_format_non_verbose(self):
        """Test CLI format warning output (non-verbose)."""
        output = StringIO()

        with patch('sys.stderr', output):
            self.logger.warn_image_skipped(
                file_path="docs/article.md",
                image_ref="images/missing.png",
                reason="File not found",
                verbose=False
            )

        result = output.getvalue()
        # Should NOT produce output in non-verbose mode
        assert result == ""

    def test_warn_image_skipped_cli_format_verbose(self):
        """Test CLI format warning output (verbose mode)."""
        output = StringIO()

        with patch('sys.stderr', output):
            self.logger.warn_image_skipped(
                file_path="docs/article.md",
                image_ref="images/missing.png",
                reason="File not found",
                verbose=True
            )

        result = output.getvalue()
        assert "[WARN]" in result
        assert "docs/article.md" in result
        assert "images/missing.png" in result
        assert "File not found" in result

    def test_cli_format_structure(self):
        """Test CLI format has correct multi-line structure."""
        output = StringIO()

        with patch('sys.stderr', output):
            self.logger.warn_image_skipped(
                file_path="docs/article.md",
                image_ref="images/missing.png",
                reason="File not found",
                verbose=True
            )

        result = output.getvalue()
        lines = result.strip().split('\n')

        # Should have multiple lines
        assert len(lines) >= 3
        # First line should have [WARN] and file path
        assert "[WARN]" in lines[0]
        assert "docs/article.md" in lines[0]
        # Should have Image: line
        assert any("Image:" in line for line in lines)
        # Should have Reason: line
        assert any("Reason:" in line for line in lines)


class TestAdaptiveLoggerServerMode:
    """Test logging in server mode."""

    def setup_method(self):
        """Set up test fixtures."""
        self.logger = AdaptiveLogger()
        self.logger._context = "server"  # Force server mode

    def test_warn_image_skipped_server_format(self):
        """Test server format uses Python logging with JSON structure."""
        # Capture log output
        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setFormatter(logging.Formatter('%(message)s'))

        # Get or create logger
        py_logger = logging.getLogger('cidx.image_extractor')
        py_logger.addHandler(handler)
        py_logger.setLevel(logging.WARNING)

        try:
            self.logger.warn_image_skipped(
                file_path="docs/article.md",
                image_ref="images/missing.png",
                reason="File not found",
                verbose=False  # Verbose flag should be ignored in server mode
            )

            result = log_capture.getvalue()

            # Should have JSON output
            assert result.strip()  # Not empty

            # Parse JSON
            log_data = json.loads(result.strip())

            # Check structure
            assert log_data["level"] == "warning"
            assert log_data["event"] == "image_skipped"
            assert log_data["file_path"] == "docs/article.md"
            assert log_data["image_ref"] == "images/missing.png"
            assert log_data["reason"] == "File not found"
        finally:
            py_logger.removeHandler(handler)

    def test_server_format_verbose_flag_ignored(self):
        """Test that verbose flag is ignored in server mode."""
        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setFormatter(logging.Formatter('%(message)s'))

        py_logger = logging.getLogger('cidx.image_extractor')
        py_logger.addHandler(handler)
        py_logger.setLevel(logging.WARNING)

        try:
            # Should log regardless of verbose flag
            self.logger.warn_image_skipped(
                file_path="docs/article.md",
                image_ref="images/missing.png",
                reason="File not found",
                verbose=False
            )

            result = log_capture.getvalue()
            assert result.strip()  # Should have output even with verbose=False
        finally:
            py_logger.removeHandler(handler)


class TestAdaptiveLoggerIntegration:
    """Integration tests for AdaptiveLogger."""

    def test_multiple_warnings_cli_mode(self):
        """Test multiple warnings in CLI mode."""
        logger = AdaptiveLogger()
        logger._context = "cli"

        output = StringIO()
        with patch('sys.stderr', output):
            logger.warn_image_skipped("file1.md", "img1.png", "missing", verbose=True)
            logger.warn_image_skipped("file2.md", "img2.png", "oversized", verbose=True)

        result = output.getvalue()
        assert "file1.md" in result
        assert "img1.png" in result
        assert "missing" in result
        assert "file2.md" in result
        assert "img2.png" in result
        assert "oversized" in result

    def test_multiple_warnings_server_mode(self):
        """Test multiple warnings in server mode produce multiple JSON logs."""
        logger = AdaptiveLogger()
        logger._context = "server"

        log_capture = StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setFormatter(logging.Formatter('%(message)s'))

        py_logger = logging.getLogger('cidx.image_extractor')
        py_logger.addHandler(handler)
        py_logger.setLevel(logging.WARNING)

        try:
            logger.warn_image_skipped("file1.md", "img1.png", "missing", verbose=False)
            logger.warn_image_skipped("file2.md", "img2.png", "oversized", verbose=False)

            result = log_capture.getvalue()
            lines = [line.strip() for line in result.strip().split('\n') if line.strip()]

            # Should have 2 JSON log entries
            assert len(lines) == 2

            # Both should be valid JSON
            log1 = json.loads(lines[0])
            log2 = json.loads(lines[1])

            assert log1["file_path"] == "file1.md"
            assert log2["file_path"] == "file2.md"
        finally:
            py_logger.removeHandler(handler)
