"""
Unit tests for ContentLimitsConfig - Story #32 Unified Content Limits Configuration.

Tests follow TDD methodology - written BEFORE implementation.

AC1: Merge configuration sections into unified "Content Limits" section
AC2: Standardize on token-based units
AC3: Remove PayloadCacheConfig.from_env() method
AC4: Remove environment variable overrides from from_server_config()
AC5: Remove MultiSearchConfig.from_env() method
AC6: Automatic configuration migration
AC7: Clear setting descriptions in Web UI
"""

import pytest


class TestContentLimitsConfigDataclass:
    """Tests for ContentLimitsConfig dataclass (AC1, AC2)."""

    def test_content_limits_config_exists(self):
        """Test that ContentLimitsConfig dataclass exists."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        assert ContentLimitsConfig is not None

    def test_content_limits_config_has_chars_per_token(self):
        """Test ContentLimitsConfig has chars_per_token field (AC2)."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig()
        assert hasattr(config, "chars_per_token")
        assert config.chars_per_token == 4  # Default value

    def test_content_limits_config_has_file_content_max_tokens(self):
        """Test ContentLimitsConfig has file_content_max_tokens field (AC2)."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig()
        assert hasattr(config, "file_content_max_tokens")
        assert config.file_content_max_tokens == 50000  # Default value

    def test_content_limits_config_has_git_diff_max_tokens(self):
        """Test ContentLimitsConfig has git_diff_max_tokens field (AC2)."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig()
        assert hasattr(config, "git_diff_max_tokens")
        assert config.git_diff_max_tokens == 50000  # Default value

    def test_content_limits_config_has_git_log_max_tokens(self):
        """Test ContentLimitsConfig has git_log_max_tokens field (AC2)."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig()
        assert hasattr(config, "git_log_max_tokens")
        assert config.git_log_max_tokens == 50000  # Default value

    def test_content_limits_config_has_search_result_max_tokens(self):
        """Test ContentLimitsConfig has search_result_max_tokens field (AC2)."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig()
        assert hasattr(config, "search_result_max_tokens")
        assert config.search_result_max_tokens == 50000  # Default value

    def test_content_limits_config_has_cache_ttl_seconds(self):
        """Test ContentLimitsConfig has cache_ttl_seconds field."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig()
        assert hasattr(config, "cache_ttl_seconds")
        assert config.cache_ttl_seconds == 3600  # Default value (1 hour)

    def test_content_limits_config_has_cache_max_entries(self):
        """Test ContentLimitsConfig has cache_max_entries field."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig()
        assert hasattr(config, "cache_max_entries")
        assert config.cache_max_entries == 10000  # Default value

    def test_content_limits_config_custom_values(self):
        """Test ContentLimitsConfig accepts custom values."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        config = ContentLimitsConfig(
            chars_per_token=5,
            file_content_max_tokens=40000,
            git_diff_max_tokens=30000,
            git_log_max_tokens=20000,
            search_result_max_tokens=25000,
            cache_ttl_seconds=7200,
            cache_max_entries=5000,
        )

        assert config.chars_per_token == 5
        assert config.file_content_max_tokens == 40000
        assert config.git_diff_max_tokens == 30000
        assert config.git_log_max_tokens == 20000
        assert config.search_result_max_tokens == 25000
        assert config.cache_ttl_seconds == 7200
        assert config.cache_max_entries == 5000


class TestServerConfigHasContentLimitsConfig:
    """Tests that ServerConfig includes ContentLimitsConfig (AC1)."""

    def test_server_config_has_content_limits_config(self, tmp_path):
        """Test that ServerConfig has content_limits_config field."""
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir=str(tmp_path))
        assert hasattr(config, "content_limits_config")
        assert config.content_limits_config is not None

    def test_server_config_content_limits_config_initialized_on_post_init(
        self, tmp_path
    ):
        """Test that content_limits_config is auto-created by __post_init__."""
        from code_indexer.server.utils.config_manager import (
            ContentLimitsConfig,
            ServerConfig,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        assert isinstance(config.content_limits_config, ContentLimitsConfig)


class TestPayloadCacheConfigFromEnvRemoved:
    """Tests that PayloadCacheConfig.from_env() is removed (AC3)."""

    def test_payload_cache_config_has_no_from_env_method(self):
        """Test that PayloadCacheConfig does NOT have from_env class method (AC3)."""
        from code_indexer.server.cache.payload_cache import PayloadCacheConfig

        # from_env should NOT exist
        assert not hasattr(PayloadCacheConfig, "from_env")


class TestPayloadCacheConfigNoEnvOverrides:
    """Tests that from_server_config has no env var overrides (AC4)."""

    def test_from_server_config_ignores_env_vars(self):
        """Test that from_server_config ignores environment variables (AC4)."""
        import os
        from unittest.mock import patch

        from code_indexer.server.cache.payload_cache import PayloadCacheConfig
        from code_indexer.server.utils.config_manager import CacheConfig

        cache_config = CacheConfig(
            payload_preview_size_chars=3000,
            payload_max_fetch_size_chars=6000,
            payload_cache_ttl_seconds=1200,
            payload_cleanup_interval_seconds=90,
        )

        # Set environment variables that should be IGNORED
        with patch.dict(
            os.environ,
            {
                "CIDX_PREVIEW_SIZE_CHARS": "9999",
                "CIDX_MAX_FETCH_SIZE_CHARS": "9999",
                "CIDX_CACHE_TTL_SECONDS": "9999",
                "CIDX_CLEANUP_INTERVAL_SECONDS": "9999",
            },
        ):
            config = PayloadCacheConfig.from_server_config(cache_config)

        # Values should come from cache_config, NOT env vars
        assert config.preview_size_chars == 3000  # NOT 9999
        assert config.max_fetch_size_chars == 6000  # NOT 9999
        assert config.cache_ttl_seconds == 1200  # NOT 9999
        assert config.cleanup_interval_seconds == 90  # NOT 9999


class TestMultiSearchConfigFromEnvRemoved:
    """Tests that MultiSearchConfig.from_env() is removed (AC5)."""

    def test_multi_search_config_has_no_from_env_method(self):
        """Test that MultiSearchConfig does NOT have from_env class method (AC5)."""
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig

        # from_env should NOT exist
        assert not hasattr(MultiSearchConfig, "from_env")


class TestConfigurationMigration:
    """Tests for automatic configuration migration (AC6)."""

    def test_migration_preserves_file_content_limits_values(self, tmp_path):
        """Test that migration preserves existing file_content_limits values."""
        import json

        from code_indexer.server.utils.config_manager import ServerConfigManager

        # Write old-format config with file_content_limits
        config_file = tmp_path / "config.json"
        old_config = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "file_content_limits_config": {
                "max_tokens_per_request": 8000,
                "chars_per_token": 5,
            },
        }
        config_file.write_text(json.dumps(old_config))

        # Load config
        manager = ServerConfigManager(str(tmp_path))
        config = manager.load_config()

        # Verify content_limits_config exists with migrated values
        assert config.content_limits_config is not None
        # The migration should preserve the old values
        assert config.content_limits_config.chars_per_token == 5
        assert config.content_limits_config.file_content_max_tokens == 8000

    def test_migration_preserves_cache_payload_values(self, tmp_path):
        """Test that migration preserves existing cache payload values."""
        import json

        from code_indexer.server.utils.config_manager import ServerConfigManager

        # Write old-format config with cache_config payload settings
        config_file = tmp_path / "config.json"
        old_config = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "cache_config": {
                "index_cache_ttl_minutes": 10.0,
                "payload_cache_ttl_seconds": 1800,
                "payload_cleanup_interval_seconds": 120,
            },
        }
        config_file.write_text(json.dumps(old_config))

        # Load config
        manager = ServerConfigManager(str(tmp_path))
        config = manager.load_config()

        # Verify content_limits_config has migrated cache values
        assert config.content_limits_config is not None
        assert config.content_limits_config.cache_ttl_seconds == 1800

    def test_migration_is_idempotent(self, tmp_path):
        """Test that migration can be run multiple times safely (idempotent)."""
        import json

        from code_indexer.server.utils.config_manager import ServerConfigManager

        # Write old-format config
        config_file = tmp_path / "config.json"
        old_config = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "file_content_limits_config": {
                "max_tokens_per_request": 8000,
                "chars_per_token": 5,
            },
        }
        config_file.write_text(json.dumps(old_config))

        manager = ServerConfigManager(str(tmp_path))

        # Load and save multiple times
        config1 = manager.load_config()
        manager.save_config(config1)
        config2 = manager.load_config()
        manager.save_config(config2)
        config3 = manager.load_config()

        # Values should be preserved through all iterations
        assert config1.content_limits_config.chars_per_token == 5
        assert config2.content_limits_config.chars_per_token == 5
        assert config3.content_limits_config.chars_per_token == 5

    def test_migration_uses_defaults_for_missing_values(self, tmp_path):
        """Test that migration uses defaults for values not in old config."""
        import json

        from code_indexer.server.utils.config_manager import ServerConfigManager

        # Write minimal config without file_content_limits or cache settings
        config_file = tmp_path / "config.json"
        old_config = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
        }
        config_file.write_text(json.dumps(old_config))

        # Load config
        manager = ServerConfigManager(str(tmp_path))
        config = manager.load_config()

        # Verify content_limits_config uses defaults
        assert config.content_limits_config is not None
        assert config.content_limits_config.chars_per_token == 4  # default
        assert config.content_limits_config.file_content_max_tokens == 50000  # default
        assert config.content_limits_config.cache_ttl_seconds == 3600  # default


class TestContentLimitsConfigValidation:
    """Tests for ContentLimitsConfig validation."""

    def test_chars_per_token_validation_range(self, tmp_path):
        """Test chars_per_token validation (1-10 range)."""
        from code_indexer.server.utils.config_manager import (
            ContentLimitsConfig,
            ServerConfig,
            ServerConfigManager,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        config.content_limits_config = ContentLimitsConfig(chars_per_token=11)

        manager = ServerConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="chars_per_token must be between 1 and 10"):
            manager.validate_config(config)

    def test_chars_per_token_validation_minimum(self, tmp_path):
        """Test chars_per_token validation minimum (1)."""
        from code_indexer.server.utils.config_manager import (
            ContentLimitsConfig,
            ServerConfig,
            ServerConfigManager,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        config.content_limits_config = ContentLimitsConfig(chars_per_token=0)

        manager = ServerConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="chars_per_token must be between 1 and 10"):
            manager.validate_config(config)

    def test_file_content_max_tokens_validation_range(self, tmp_path):
        """Test file_content_max_tokens validation (1000-200000 range)."""
        from code_indexer.server.utils.config_manager import (
            ContentLimitsConfig,
            ServerConfig,
            ServerConfigManager,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        config.content_limits_config = ContentLimitsConfig(file_content_max_tokens=500)

        manager = ServerConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="file_content_max_tokens must be between"):
            manager.validate_config(config)

    def test_cache_ttl_seconds_validation_minimum(self, tmp_path):
        """Test cache_ttl_seconds validation (minimum 60)."""
        from code_indexer.server.utils.config_manager import (
            ContentLimitsConfig,
            ServerConfig,
            ServerConfigManager,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        config.content_limits_config = ContentLimitsConfig(cache_ttl_seconds=30)

        manager = ServerConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="cache_ttl_seconds must be"):
            manager.validate_config(config)

    def test_cache_max_entries_validation_range(self, tmp_path):
        """Test cache_max_entries validation (100-100000 range)."""
        from code_indexer.server.utils.config_manager import (
            ContentLimitsConfig,
            ServerConfig,
            ServerConfigManager,
        )

        config = ServerConfig(server_dir=str(tmp_path))
        config.content_limits_config = ContentLimitsConfig(cache_max_entries=50)

        manager = ServerConfigManager(str(tmp_path))
        with pytest.raises(ValueError, match="cache_max_entries must be between"):
            manager.validate_config(config)


class TestContentLimitsConfigPersistence:
    """Tests for ContentLimitsConfig JSON serialization/deserialization."""

    def test_content_limits_config_saves_to_json(self, tmp_path):
        """Test that content_limits_config is saved to config.json."""
        import json

        from code_indexer.server.utils.config_manager import (
            ContentLimitsConfig,
            ServerConfigManager,
        )

        manager = ServerConfigManager(str(tmp_path))
        config = manager.create_default_config()
        config.content_limits_config = ContentLimitsConfig(
            chars_per_token=5,
            file_content_max_tokens=40000,
            cache_ttl_seconds=7200,
        )

        manager.save_config(config)

        # Read raw JSON and verify structure
        config_file = tmp_path / "config.json"
        saved_data = json.loads(config_file.read_text())

        assert "content_limits_config" in saved_data
        assert saved_data["content_limits_config"]["chars_per_token"] == 5
        assert saved_data["content_limits_config"]["file_content_max_tokens"] == 40000
        assert saved_data["content_limits_config"]["cache_ttl_seconds"] == 7200

    def test_content_limits_config_loads_from_json(self, tmp_path):
        """Test that content_limits_config is loaded from config.json."""
        import json

        from code_indexer.server.utils.config_manager import ServerConfigManager

        # Write config file with content_limits_config
        config_file = tmp_path / "config.json"
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "content_limits_config": {
                "chars_per_token": 6,
                "file_content_max_tokens": 35000,
                "git_diff_max_tokens": 25000,
                "git_log_max_tokens": 15000,
                "search_result_max_tokens": 45000,
                "cache_ttl_seconds": 5400,
                "cache_max_entries": 8000,
            },
        }
        config_file.write_text(json.dumps(config_data))

        # Load config
        manager = ServerConfigManager(str(tmp_path))
        config = manager.load_config()

        # Verify loaded values
        assert config.content_limits_config.chars_per_token == 6
        assert config.content_limits_config.file_content_max_tokens == 35000
        assert config.content_limits_config.git_diff_max_tokens == 25000
        assert config.content_limits_config.git_log_max_tokens == 15000
        assert config.content_limits_config.search_result_max_tokens == 45000
        assert config.content_limits_config.cache_ttl_seconds == 5400
        assert config.content_limits_config.cache_max_entries == 8000
