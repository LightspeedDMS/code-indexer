"""
Unit tests for hnsw_max_elements configuration - Story #588.

Tests that hnsw_max_elements is added to ServerResourceConfig with default 1000000.
"""

from dataclasses import asdict

from code_indexer.server.utils.config_manager import ServerResourceConfig


class TestResourceConfigHnswMaxElements:
    """Test hnsw_max_elements field on ServerResourceConfig."""

    def test_resource_config_has_hnsw_max_elements(self):
        """Field exists on ServerResourceConfig with default 1000000."""
        config = ServerResourceConfig()
        assert hasattr(config, "hnsw_max_elements")
        assert config.hnsw_max_elements == 1000000

    def test_default_value_is_one_million(self):
        """Default is 1000000, not the old hardcoded 500000."""
        config = ServerResourceConfig()
        assert config.hnsw_max_elements == 1000000
        assert config.hnsw_max_elements != 500000

    def test_resource_config_custom_value(self):
        """Field accepts custom values."""
        config = ServerResourceConfig(hnsw_max_elements=2000000)
        assert config.hnsw_max_elements == 2000000

    def test_serialization_round_trip(self):
        """hnsw_max_elements survives asdict() serialization."""
        config = ServerResourceConfig(hnsw_max_elements=750000)
        d = asdict(config)
        assert d["hnsw_max_elements"] == 750000
        restored = ServerResourceConfig(**d)
        assert restored.hnsw_max_elements == 750000
