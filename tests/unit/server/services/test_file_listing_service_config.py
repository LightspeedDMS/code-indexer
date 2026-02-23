"""
Unit tests for FileListingService._is_file_indexed() using server config (Story #223 - AC3).

Tests that the file listing service uses the server's indexable_extensions config
instead of a hardcoded set, and that there is no hidden fallback to the old set.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from code_indexer.server.services.config_service import ConfigService, reset_config_service
from code_indexer.server.services.file_service import FileListingService


class TestFileListingServiceUsesConfig:
    """Tests that _is_file_indexed() reads from server config (AC3)."""

    def setup_method(self):
        """Setup temp config dir and patch the global config service."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up temp dir and reset global config service singleton."""
        reset_config_service()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _make_service_with_config(self, extensions):
        """Helper: configure extensions and return a FileListingService."""
        self.config_service.update_setting(
            category="indexing",
            key="indexable_extensions",
            value=extensions,
        )
        service = FileListingService.__new__(FileListingService)
        return service

    def test_returns_true_for_configured_extension(self):
        """AC3: _is_file_indexed returns True when extension is in config."""
        service = self._make_service_with_config([".py", ".go"])
        with patch(
            "code_indexer.server.services.file_service.get_config_service",
            return_value=self.config_service,
        ):
            result = service._is_file_indexed(Path("some_file.py"))
        assert result is True

    def test_returns_false_for_unconfigured_extension(self):
        """AC3: _is_file_indexed returns False when extension is NOT in config."""
        service = self._make_service_with_config([".py", ".go"])
        with patch(
            "code_indexer.server.services.file_service.get_config_service",
            return_value=self.config_service,
        ):
            result = service._is_file_indexed(Path("document.pdf"))
        assert result is False

    def test_respects_unusual_configured_extension(self):
        """AC3: _is_file_indexed respects a non-standard extension in config."""
        service = self._make_service_with_config([".cobol", ".cob"])
        with patch(
            "code_indexer.server.services.file_service.get_config_service",
            return_value=self.config_service,
        ):
            result = service._is_file_indexed(Path("program.cobol"))
        assert result is True

    def test_returns_false_when_only_unusual_extension_configured(self):
        """AC3: Standard extensions return False when config only has unusual ones."""
        service = self._make_service_with_config([".cobol"])
        with patch(
            "code_indexer.server.services.file_service.get_config_service",
            return_value=self.config_service,
        ):
            result = service._is_file_indexed(Path("script.py"))
        assert result is False


class TestNoHiddenFallback:
    """Tests that there is no fallback to the old hardcoded set (AC3)."""

    def setup_method(self):
        """Setup temp config dir."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up temp dir and reset global config service singleton."""
        reset_config_service()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_empty_config_means_nothing_indexed(self):
        """AC3: When indexable_extensions is empty, no file is considered indexed."""
        self.config_service.update_setting(
            category="indexing",
            key="indexable_extensions",
            value=[],
        )
        service = FileListingService.__new__(FileListingService)
        with patch(
            "code_indexer.server.services.file_service.get_config_service",
            return_value=self.config_service,
        ):
            # These were in the old hardcoded set - must return False now
            assert service._is_file_indexed(Path("script.py")) is False
            assert service._is_file_indexed(Path("app.js")) is False
            assert service._is_file_indexed(Path("readme.md")) is False

    def test_no_fallback_to_old_hardcoded_set(self):
        """AC3: Extensions not in config must NOT fall back to old hardcoded set."""
        # Configure only .go - old set had .py, .js, etc.
        self.config_service.update_setting(
            category="indexing",
            key="indexable_extensions",
            value=[".go"],
        )
        service = FileListingService.__new__(FileListingService)
        with patch(
            "code_indexer.server.services.file_service.get_config_service",
            return_value=self.config_service,
        ):
            # .py was in old hardcoded set - must return False when not in config
            assert service._is_file_indexed(Path("script.py")) is False
            # .go IS in config - must return True
            assert service._is_file_indexed(Path("main.go")) is True
