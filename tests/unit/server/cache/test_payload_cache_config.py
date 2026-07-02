"""Unit tests for PayloadCacheConfig.

Story #679: S1 - Semantic Search with Payload Control (Foundation)
AC1: Configuration Parameters

Story #32: Removed environment variable configuration. All configuration
must come from Web UI configuration system (ServerConfig).
"""


class TestPayloadCacheConfig:
    """Tests for PayloadCacheConfig dataclass (AC1)."""

    def test_default_values(self):
        """Test that PayloadCacheConfig has correct default values."""
        from code_indexer.server.cache.payload_cache import PayloadCacheConfig

        config = PayloadCacheConfig()

        assert config.preview_size_chars == 2000
        assert config.max_fetch_size_chars == 5000
        assert config.cache_ttl_seconds == 900
        assert config.cleanup_interval_seconds == 60

    def test_custom_values(self):
        """Test that PayloadCacheConfig accepts custom values."""
        from code_indexer.server.cache.payload_cache import PayloadCacheConfig

        config = PayloadCacheConfig(
            preview_size_chars=1000,
            max_fetch_size_chars=3000,
            cache_ttl_seconds=300,
            cleanup_interval_seconds=30,
        )

        assert config.preview_size_chars == 1000
        assert config.max_fetch_size_chars == 3000
        assert config.cache_ttl_seconds == 300
        assert config.cleanup_interval_seconds == 30

    def test_from_server_config_uses_cache_config_values(self):
        """Test that from_server_config uses values from CacheConfig."""
        from code_indexer.server.cache.payload_cache import PayloadCacheConfig
        from code_indexer.server.utils.config_manager import CacheConfig

        cache_config = CacheConfig(
            payload_preview_size_chars=3000,
            payload_max_fetch_size_chars=6000,
            payload_cache_ttl_seconds=1200,
            payload_cleanup_interval_seconds=90,
        )

        config = PayloadCacheConfig.from_server_config(cache_config)

        assert config.preview_size_chars == 3000
        assert config.max_fetch_size_chars == 6000
        assert config.cache_ttl_seconds == 1200
        assert config.cleanup_interval_seconds == 90

    def test_from_server_config_with_none_uses_defaults(self):
        """Test that from_server_config with None cache_config uses defaults."""
        from code_indexer.server.cache.payload_cache import PayloadCacheConfig

        config = PayloadCacheConfig.from_server_config(None)

        assert config.preview_size_chars == 2000
        assert config.max_fetch_size_chars == 5000
        assert config.cache_ttl_seconds == 900
        assert config.cleanup_interval_seconds == 60
