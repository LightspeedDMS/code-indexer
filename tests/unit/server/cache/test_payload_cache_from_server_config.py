"""
Unit tests for PayloadCacheConfig.from_server_config method.

Story #679: Add Payload Cache Settings to Web UI Config Screen
Story #32: Removed environment variable overrides. All configuration
must come from Web UI configuration system (ServerConfig).
"""

from code_indexer.server.cache.payload_cache import PayloadCacheConfig
from code_indexer.server.utils.config_manager import CacheConfig


class TestPayloadCacheFromServerConfig:
    """Tests for PayloadCacheConfig.from_server_config class method."""

    def test_from_server_config_uses_cache_config_values(self):
        """Test that from_server_config uses values from CacheConfig."""
        cache_config = CacheConfig(
            payload_preview_size_chars=3000,
            payload_max_fetch_size_chars=8000,
            payload_cache_ttl_seconds=1200,
            payload_cleanup_interval_seconds=90,
        )

        config = PayloadCacheConfig.from_server_config(cache_config)

        assert config.preview_size_chars == 3000
        assert config.max_fetch_size_chars == 8000
        assert config.cache_ttl_seconds == 1200
        assert config.cleanup_interval_seconds == 90

    def test_from_server_config_with_default_cache_config(self):
        """Test that from_server_config works with default CacheConfig values."""
        cache_config = CacheConfig()  # All defaults

        config = PayloadCacheConfig.from_server_config(cache_config)

        assert config.preview_size_chars == 2000
        assert config.max_fetch_size_chars == 5000
        assert config.cache_ttl_seconds == 900
        assert config.cleanup_interval_seconds == 60

    def test_from_server_config_with_none_uses_defaults(self):
        """Test that from_server_config with None cache_config uses defaults."""
        config = PayloadCacheConfig.from_server_config(None)

        assert config.preview_size_chars == 2000
        assert config.max_fetch_size_chars == 5000
        assert config.cache_ttl_seconds == 900
        assert config.cleanup_interval_seconds == 60
