"""
Unit tests for IndexingConfig.indexable_extensions field (Story #223 - AC1, AC4, AC5).

Tests the new indexable_extensions field on IndexingConfig dataclass and ConfigService
integration for reading, updating, and persisting the setting.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import os
import shutil
import tempfile

import pytest

from code_indexer.server.utils.config_manager import IndexingConfig, ServerConfig
from code_indexer.server.services.config_service import ConfigService


class TestIndexingConfigExtensions:
    """Tests for the indexable_extensions field on IndexingConfig (AC1)."""

    def test_indexing_config_has_indexable_extensions_field(self):
        """AC1: IndexingConfig must have an indexable_extensions field."""
        config = IndexingConfig()
        assert hasattr(config, "indexable_extensions")

    def test_indexable_extensions_is_list(self):
        """AC1: indexable_extensions must be a list."""
        config = IndexingConfig()
        assert isinstance(config.indexable_extensions, list)

    def test_indexable_extensions_has_60_unique_items(self):
        """AC1: Default list must contain exactly 60 unique extensions."""
        config = IndexingConfig()
        exts = config.indexable_extensions
        assert len(exts) == 60, f"Expected 60 extensions, got {len(exts)}: {exts}"
        assert len(set(exts)) == 60, "Extensions must be unique"

    def test_indexable_extensions_have_leading_dot(self):
        """AC1: All extensions must have a leading dot."""
        config = IndexingConfig()
        for ext in config.indexable_extensions:
            assert ext.startswith("."), f"Extension {ext!r} is missing leading dot"

    def test_indexable_extensions_are_lowercase(self):
        """AC1: All extensions must be lowercase."""
        config = IndexingConfig()
        for ext in config.indexable_extensions:
            assert ext == ext.lower(), f"Extension {ext!r} is not lowercase"

    def test_indexable_extensions_covers_common_languages(self):
        """AC1: Default extensions must cover common programming languages."""
        config = IndexingConfig()
        exts = set(config.indexable_extensions)
        # Core languages that must be present
        required = {".py", ".js", ".ts", ".java", ".go", ".rs", ".cs", ".rb", ".php"}
        missing = required - exts
        assert not missing, f"Missing required extensions: {missing}"

    def test_indexable_extensions_matches_cli_defaults(self):
        """AC1: Extensions must match the CLI Config.file_extensions defaults (with dots)."""
        config = IndexingConfig()
        server_exts = set(config.indexable_extensions)
        # Key CLI extensions that should be present in server config
        cli_sample = {".py", ".js", ".ts", ".java", ".go", ".rs", ".sh", ".sql", ".md"}
        missing = cli_sample - server_exts
        assert not missing, f"CLI defaults not covered: {missing}"

    def test_server_config_initializes_indexing_config_with_extensions(self):
        """AC1: ServerConfig.__post_init__ must initialize IndexingConfig with extensions."""
        config = ServerConfig(server_dir="/tmp/test-cidx-server")
        assert config.indexing_config is not None
        assert hasattr(config.indexing_config, "indexable_extensions")
        assert len(config.indexing_config.indexable_extensions) == 60


class TestIndexingConfigRoundTrip:
    """Tests that indexable_extensions survives JSON serialization (for persistence)."""

    def setup_method(self):
        """Setup temp dir for persistence tests."""
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        """Clean up temp dir."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_custom_extensions_survive_save_and_reload(self):
        """Custom indexable_extensions must survive save/reload cycle."""
        config_service = ConfigService(server_dir_path=self.temp_dir)
        custom_exts = [".py", ".ts", ".go"]
        config_service.update_setting("indexing", "indexable_extensions", custom_exts)

        # Reload from disk
        new_service = ConfigService(server_dir_path=self.temp_dir)
        settings = new_service.get_all_settings()
        assert settings["indexing"]["indexable_extensions"] == custom_exts

    def test_default_extensions_survive_json_roundtrip(self):
        """Default indexable_extensions must survive JSON serialization."""
        config = IndexingConfig()
        original_exts = list(config.indexable_extensions)

        # Serialize and deserialize via JSON
        from dataclasses import asdict
        data = asdict(config)
        json_str = json.dumps(data)
        restored_data = json.loads(json_str)
        restored = IndexingConfig(**restored_data)

        assert restored.indexable_extensions == original_exts

    def test_empty_list_survives_roundtrip(self):
        """Empty indexable_extensions list must survive JSON roundtrip."""
        config = IndexingConfig()
        config.indexable_extensions = []

        from dataclasses import asdict
        data = asdict(config)
        json_str = json.dumps(data)
        restored_data = json.loads(json_str)
        restored = IndexingConfig(**restored_data)

        assert restored.indexable_extensions == []


class TestConfigServiceIndexingSettings:
    """Tests for ConfigService indexing section (AC4, AC5)."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up test environment."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_get_all_settings_includes_indexing_section(self):
        """AC4: get_all_settings must include an 'indexing' section."""
        settings = self.config_service.get_all_settings()
        assert "indexing" in settings

    def test_get_all_settings_indexing_has_indexable_extensions_key(self):
        """AC4: 'indexing' section must have 'indexable_extensions' key."""
        settings = self.config_service.get_all_settings()
        assert "indexable_extensions" in settings["indexing"]

    def test_get_all_settings_indexing_default_has_60_items(self):
        """AC4: Default indexable_extensions in get_all_settings must have 60 items."""
        settings = self.config_service.get_all_settings()
        exts = settings["indexing"]["indexable_extensions"]
        assert len(exts) == 60, f"Expected 60 extensions, got {len(exts)}"

    def test_update_indexing_setting_with_list(self):
        """AC4: update_setting with 'indexing' category and list value must work."""
        new_exts = [".py", ".go", ".rs"]
        self.config_service.update_setting(
            category="indexing",
            key="indexable_extensions",
            value=new_exts,
        )
        settings = self.config_service.get_all_settings()
        assert settings["indexing"]["indexable_extensions"] == new_exts

    def test_update_indexing_setting_with_comma_string(self):
        """AC4: update_setting with comma-separated string must parse to list."""
        self.config_service.update_setting(
            category="indexing",
            key="indexable_extensions",
            value=".py, .go, .rs",
        )
        settings = self.config_service.get_all_settings()
        assert settings["indexing"]["indexable_extensions"] == [".py", ".go", ".rs"]

    def test_update_indexing_setting_persists_to_disk(self):
        """AC4: Updated indexable_extensions must persist to disk."""
        new_exts = [".py", ".ts"]
        self.config_service.update_setting(
            category="indexing",
            key="indexable_extensions",
            value=new_exts,
        )

        # New instance reads from disk
        new_service = ConfigService(server_dir_path=self.temp_dir)
        settings = new_service.get_all_settings()
        assert settings["indexing"]["indexable_extensions"] == new_exts

    def test_update_indexing_unknown_key_raises_value_error(self):
        """AC4: Unknown key in 'indexing' category must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown indexing setting"):
            self.config_service.update_setting(
                category="indexing",
                key="nonexistent_key",
                value="whatever",
            )
