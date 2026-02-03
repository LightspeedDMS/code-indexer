"""
Unit tests for Configuration Consolidation (Story #3 - Phase 1).

Tests for migrating static configuration from SQLite databases and separate JSON files
to the main config.json. Tests the new dataclasses: SearchLimitsConfig, FileContentLimitsConfig,
and GoldenReposConfig.
"""

import pytest

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    SearchLimitsConfig,
    FileContentLimitsConfig,
    GoldenReposConfig,
)
from code_indexer.server.services.config_service import ConfigService


class TestSearchLimitsConfig:
    """Test suite for SearchLimitsConfig dataclass (AC-M1, AC-M2)."""

    def test_default_values(self):
        """Test SearchLimitsConfig has correct default values."""
        config = SearchLimitsConfig()

        # AC-M1: Default 1 MB
        assert config.max_result_size_mb == 1
        # AC-M2: Default 30 seconds
        assert config.timeout_seconds == 30

    def test_custom_values(self):
        """Test SearchLimitsConfig accepts custom values."""
        config = SearchLimitsConfig(max_result_size_mb=50, timeout_seconds=120)

        assert config.max_result_size_mb == 50
        assert config.timeout_seconds == 120

    def test_max_size_bytes_property(self):
        """Test max_size_bytes computed property."""
        config = SearchLimitsConfig(max_result_size_mb=10)

        # 10 MB = 10 * 1024 * 1024 bytes
        assert config.max_size_bytes == 10 * 1024 * 1024


class TestFileContentLimitsConfig:
    """Test suite for FileContentLimitsConfig dataclass (AC-M3, AC-M4)."""

    def test_default_values(self):
        """Test FileContentLimitsConfig has correct default values."""
        config = FileContentLimitsConfig()

        # AC-M3: Default 5000 tokens
        assert config.max_tokens_per_request == 5000
        # AC-M4: Default 4 chars/token
        assert config.chars_per_token == 4

    def test_custom_values(self):
        """Test FileContentLimitsConfig accepts custom values."""
        config = FileContentLimitsConfig(
            max_tokens_per_request=10000, chars_per_token=3
        )

        assert config.max_tokens_per_request == 10000
        assert config.chars_per_token == 3

    def test_max_chars_per_request_property(self):
        """Test max_chars_per_request computed property."""
        config = FileContentLimitsConfig(max_tokens_per_request=5000, chars_per_token=4)

        # 5000 * 4 = 20000 chars
        assert config.max_chars_per_request == 20000


class TestGoldenReposConfig:
    """Test suite for GoldenReposConfig dataclass (AC-M5)."""

    def test_default_values(self):
        """Test GoldenReposConfig has correct default values."""
        config = GoldenReposConfig()

        # AC-M5: Default 3600 seconds (1 hour)
        assert config.refresh_interval_seconds == 3600

    def test_custom_values(self):
        """Test GoldenReposConfig accepts custom values."""
        config = GoldenReposConfig(refresh_interval_seconds=1800)

        assert config.refresh_interval_seconds == 1800


class TestServerConfigIntegration:
    """Test suite for ServerConfig integration with new config objects."""

    def test_server_config_includes_search_limits(self):
        """Test ServerConfig includes search_limits_config with defaults."""
        config = ServerConfig(server_dir="/tmp/test")

        assert config.search_limits_config is not None
        assert config.search_limits_config.max_result_size_mb == 1
        assert config.search_limits_config.timeout_seconds == 30

    def test_server_config_includes_file_content_limits(self):
        """Test ServerConfig includes file_content_limits_config with defaults."""
        config = ServerConfig(server_dir="/tmp/test")

        assert config.file_content_limits_config is not None
        assert config.file_content_limits_config.max_tokens_per_request == 5000
        assert config.file_content_limits_config.chars_per_token == 4

    def test_server_config_includes_golden_repos(self):
        """Test ServerConfig includes golden_repos_config with defaults."""
        config = ServerConfig(server_dir="/tmp/test")

        assert config.golden_repos_config is not None
        assert config.golden_repos_config.refresh_interval_seconds == 3600

    def test_server_config_save_load_round_trip(self, tmp_path):
        """Test new configs survive JSON serialization round-trip."""
        # Create manager with temp directory
        manager = ServerConfigManager(str(tmp_path))

        # Create config with custom values
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_limits_config=SearchLimitsConfig(
                max_result_size_mb=50,
                timeout_seconds=120,
            ),
            file_content_limits_config=FileContentLimitsConfig(
                max_tokens_per_request=10000,
                chars_per_token=3,
            ),
            golden_repos_config=GoldenReposConfig(
                refresh_interval_seconds=1800,
            ),
        )

        # Save to disk
        manager.save_config(config)

        # Load from disk
        loaded = manager.load_config()

        # Verify all values survived round-trip
        assert loaded is not None
        assert loaded.search_limits_config.max_result_size_mb == 50
        assert loaded.search_limits_config.timeout_seconds == 120
        assert loaded.file_content_limits_config.max_tokens_per_request == 10000
        assert loaded.file_content_limits_config.chars_per_token == 3
        assert loaded.golden_repos_config.refresh_interval_seconds == 1800


class TestConfigServiceIntegration:
    """Test suite for ConfigService integration with new config settings."""

    def test_get_all_settings_includes_search_limits(self, tmp_path):
        """Test get_all_settings() returns search_limits section."""
        service = ConfigService(str(tmp_path))
        settings = service.get_all_settings()

        assert "search_limits" in settings
        assert settings["search_limits"]["max_result_size_mb"] == 1
        assert settings["search_limits"]["timeout_seconds"] == 30

    def test_get_all_settings_includes_file_content_limits(self, tmp_path):
        """Test get_all_settings() returns file_content_limits section."""
        service = ConfigService(str(tmp_path))
        settings = service.get_all_settings()

        assert "file_content_limits" in settings
        assert settings["file_content_limits"]["max_tokens_per_request"] == 5000
        assert settings["file_content_limits"]["chars_per_token"] == 4

    def test_get_all_settings_includes_golden_repos(self, tmp_path):
        """Test get_all_settings() returns golden_repos section."""
        service = ConfigService(str(tmp_path))
        settings = service.get_all_settings()

        assert "golden_repos" in settings
        assert settings["golden_repos"]["refresh_interval_seconds"] == 3600

    def test_update_search_limits_setting(self, tmp_path):
        """Test update_setting() can modify search_limits settings."""
        service = ConfigService(str(tmp_path))

        # Update max_result_size_mb
        service.update_setting("search_limits", "max_result_size_mb", 50)
        settings = service.get_all_settings()
        assert settings["search_limits"]["max_result_size_mb"] == 50

        # Update timeout_seconds
        service.update_setting("search_limits", "timeout_seconds", 120)
        settings = service.get_all_settings()
        assert settings["search_limits"]["timeout_seconds"] == 120

    def test_update_file_content_limits_setting(self, tmp_path):
        """Test update_setting() can modify file_content_limits settings."""
        service = ConfigService(str(tmp_path))

        # Update max_tokens_per_request
        service.update_setting("file_content_limits", "max_tokens_per_request", 10000)
        settings = service.get_all_settings()
        assert settings["file_content_limits"]["max_tokens_per_request"] == 10000

        # Update chars_per_token
        service.update_setting("file_content_limits", "chars_per_token", 3)
        settings = service.get_all_settings()
        assert settings["file_content_limits"]["chars_per_token"] == 3

    def test_update_golden_repos_setting(self, tmp_path):
        """Test update_setting() can modify golden_repos settings."""
        service = ConfigService(str(tmp_path))

        # Update refresh_interval_seconds
        service.update_setting("golden_repos", "refresh_interval_seconds", 1800)
        settings = service.get_all_settings()
        assert settings["golden_repos"]["refresh_interval_seconds"] == 1800


class TestConfigValidation:
    """Test suite for configuration validation rules (Story #3 - Phase 1)."""

    def test_search_limits_max_result_size_mb_valid_range(self, tmp_path):
        """Test max_result_size_mb accepts valid range (1-100 MB)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_limits_config=SearchLimitsConfig(max_result_size_mb=50),
        )
        manager.validate_config(config)  # Should not raise

    def test_search_limits_max_result_size_mb_invalid_below_min(self, tmp_path):
        """Test max_result_size_mb rejects values below minimum (< 1 MB)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_limits_config=SearchLimitsConfig(max_result_size_mb=0),
        )
        with pytest.raises(ValueError, match="max_result_size_mb"):
            manager.validate_config(config)

    def test_search_limits_max_result_size_mb_invalid_above_max(self, tmp_path):
        """Test max_result_size_mb rejects values above maximum (> 100 MB)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_limits_config=SearchLimitsConfig(max_result_size_mb=101),
        )
        with pytest.raises(ValueError, match="max_result_size_mb"):
            manager.validate_config(config)

    def test_search_limits_timeout_seconds_valid_range(self, tmp_path):
        """Test timeout_seconds accepts valid range (5-300 seconds)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_limits_config=SearchLimitsConfig(timeout_seconds=60),
        )
        manager.validate_config(config)  # Should not raise

    def test_search_limits_timeout_seconds_invalid_below_min(self, tmp_path):
        """Test timeout_seconds rejects values below minimum (< 5 seconds)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_limits_config=SearchLimitsConfig(timeout_seconds=4),
        )
        with pytest.raises(ValueError, match="timeout_seconds"):
            manager.validate_config(config)

    def test_search_limits_timeout_seconds_invalid_above_max(self, tmp_path):
        """Test timeout_seconds rejects values above maximum (> 300 seconds)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_limits_config=SearchLimitsConfig(timeout_seconds=301),
        )
        with pytest.raises(ValueError, match="timeout_seconds"):
            manager.validate_config(config)

    def test_file_content_limits_max_tokens_valid_range(self, tmp_path):
        """Test max_tokens_per_request accepts valid range (1000-50000 tokens)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            file_content_limits_config=FileContentLimitsConfig(
                max_tokens_per_request=5000
            ),
        )
        manager.validate_config(config)  # Should not raise

    def test_file_content_limits_max_tokens_invalid_below_min(self, tmp_path):
        """Test max_tokens_per_request rejects values below minimum (< 1000 tokens)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            file_content_limits_config=FileContentLimitsConfig(
                max_tokens_per_request=999
            ),
        )
        with pytest.raises(ValueError, match="max_tokens_per_request"):
            manager.validate_config(config)

    def test_file_content_limits_max_tokens_invalid_above_max(self, tmp_path):
        """Test max_tokens_per_request rejects values above maximum (> 50000 tokens)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            file_content_limits_config=FileContentLimitsConfig(
                max_tokens_per_request=50001
            ),
        )
        with pytest.raises(ValueError, match="max_tokens_per_request"):
            manager.validate_config(config)

    def test_file_content_limits_chars_per_token_valid_range(self, tmp_path):
        """Test chars_per_token accepts valid range (1-10 chars/token)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            file_content_limits_config=FileContentLimitsConfig(chars_per_token=4),
        )
        manager.validate_config(config)  # Should not raise

    def test_file_content_limits_chars_per_token_invalid_below_min(self, tmp_path):
        """Test chars_per_token rejects values below minimum (< 1 char/token)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            file_content_limits_config=FileContentLimitsConfig(chars_per_token=0),
        )
        with pytest.raises(ValueError, match="chars_per_token"):
            manager.validate_config(config)

    def test_file_content_limits_chars_per_token_invalid_above_max(self, tmp_path):
        """Test chars_per_token rejects values above maximum (> 10 chars/token)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            file_content_limits_config=FileContentLimitsConfig(chars_per_token=11),
        )
        with pytest.raises(ValueError, match="chars_per_token"):
            manager.validate_config(config)

    def test_golden_repos_refresh_interval_valid(self, tmp_path):
        """Test refresh_interval_seconds accepts valid value (>= 60 seconds)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            golden_repos_config=GoldenReposConfig(refresh_interval_seconds=3600),
        )
        manager.validate_config(config)  # Should not raise

    def test_golden_repos_refresh_interval_invalid_below_min(self, tmp_path):
        """Test refresh_interval_seconds rejects values below minimum (< 60 seconds)."""
        manager = ServerConfigManager(str(tmp_path))
        config = ServerConfig(
            server_dir=str(tmp_path),
            golden_repos_config=GoldenReposConfig(refresh_interval_seconds=59),
        )
        with pytest.raises(ValueError, match="refresh_interval_seconds"):
            manager.validate_config(config)
