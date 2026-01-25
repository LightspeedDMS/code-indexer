"""
Unit tests for MultiSearchLimitsConfig configuration (Story #25).

Tests the configuration for MultiSearchService and SCIPMultiService worker limits
that are now configurable via Web UI Configuration system.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import pytest

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    MultiSearchLimitsConfig,
)


class TestMultiSearchLimitsConfigDataclass:
    """Test suite for MultiSearchLimitsConfig dataclass (Story #25)."""

    # ==========================================================================
    # AC1: Default values per resource audit (default: 2 workers, 30s timeout)
    # ==========================================================================

    def test_default_multi_search_max_workers(self):
        """AC1: Default multi_search_max_workers should be 2 (per resource audit)."""
        config = MultiSearchLimitsConfig()
        assert config.multi_search_max_workers == 2

    def test_default_multi_search_timeout_seconds(self):
        """AC1: Default multi_search_timeout_seconds should be 30."""
        config = MultiSearchLimitsConfig()
        assert config.multi_search_timeout_seconds == 30

    def test_default_scip_multi_max_workers(self):
        """AC1: Default scip_multi_max_workers should be 2 (per resource audit)."""
        config = MultiSearchLimitsConfig()
        assert config.scip_multi_max_workers == 2

    def test_default_scip_multi_timeout_seconds(self):
        """AC1: Default scip_multi_timeout_seconds should be 30."""
        config = MultiSearchLimitsConfig()
        assert config.scip_multi_timeout_seconds == 30

    # ==========================================================================
    # Dataclass field tests
    # ==========================================================================

    def test_custom_values_initialization(self):
        """Custom values should be accepted during initialization."""
        config = MultiSearchLimitsConfig(
            multi_search_max_workers=4,
            multi_search_timeout_seconds=60,
            scip_multi_max_workers=3,
            scip_multi_timeout_seconds=45,
        )
        assert config.multi_search_max_workers == 4
        assert config.multi_search_timeout_seconds == 60
        assert config.scip_multi_max_workers == 3
        assert config.scip_multi_timeout_seconds == 45


class TestServerConfigMultiSearchIntegration:
    """Test ServerConfig integration with MultiSearchLimitsConfig."""

    # ==========================================================================
    # ServerConfig should have multi_search_limits_config field
    # ==========================================================================

    def test_server_config_has_multi_search_limits_config_field(self):
        """ServerConfig should have multi_search_limits_config field."""
        config = ServerConfig(server_dir="/tmp/test")
        assert hasattr(config, "multi_search_limits_config")

    def test_server_config_initializes_multi_search_limits_config(self):
        """ServerConfig should auto-initialize multi_search_limits_config in __post_init__."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.multi_search_limits_config is not None
        assert isinstance(config.multi_search_limits_config, MultiSearchLimitsConfig)

    def test_server_config_multi_search_limits_defaults(self):
        """ServerConfig should have default MultiSearchLimitsConfig values."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.multi_search_limits_config.multi_search_max_workers == 2
        assert config.multi_search_limits_config.multi_search_timeout_seconds == 30
        assert config.multi_search_limits_config.scip_multi_max_workers == 2
        assert config.multi_search_limits_config.scip_multi_timeout_seconds == 30


class TestServerConfigManagerMultiSearchPersistence:
    """Test ServerConfigManager save/load for MultiSearchLimitsConfig."""

    # ==========================================================================
    # Config save/load roundtrip
    # ==========================================================================

    def test_default_config_has_multi_search_limits(self, tmp_path):
        """AC1: Fresh installation should have default MultiSearchLimitsConfig."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        assert config.multi_search_limits_config is not None
        assert config.multi_search_limits_config.multi_search_max_workers == 2

    def test_custom_multi_search_config_saves_to_file(self, tmp_path):
        """Custom multi_search settings should persist when saved to config file."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Set custom values
        config.multi_search_limits_config.multi_search_max_workers = 5
        config.multi_search_limits_config.multi_search_timeout_seconds = 60
        config.multi_search_limits_config.scip_multi_max_workers = 4
        config.multi_search_limits_config.scip_multi_timeout_seconds = 45
        config_manager.save_config(config)

        # Verify it was saved to JSON
        config_file = tmp_path / "config.json"
        with open(config_file) as f:
            saved_config = json.load(f)

        assert "multi_search_limits_config" in saved_config
        assert saved_config["multi_search_limits_config"]["multi_search_max_workers"] == 5
        assert saved_config["multi_search_limits_config"]["multi_search_timeout_seconds"] == 60
        assert saved_config["multi_search_limits_config"]["scip_multi_max_workers"] == 4
        assert saved_config["multi_search_limits_config"]["scip_multi_timeout_seconds"] == 45

    def test_custom_multi_search_config_loads_from_file(self, tmp_path):
        """Custom multi_search settings should load correctly from config file."""
        # Create config file with custom multi_search settings
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "multi_search_limits_config": {
                "multi_search_max_workers": 6,
                "multi_search_timeout_seconds": 90,
                "scip_multi_max_workers": 5,
                "scip_multi_timeout_seconds": 75,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load and verify
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config.multi_search_limits_config.multi_search_max_workers == 6
        assert config.multi_search_limits_config.multi_search_timeout_seconds == 90
        assert config.multi_search_limits_config.scip_multi_max_workers == 5
        assert config.multi_search_limits_config.scip_multi_timeout_seconds == 75

    def test_config_without_multi_search_gets_defaults(self, tmp_path):
        """Old config without multi_search_limits_config should get defaults."""
        # Create config file without multi_search_limits_config (simulates old config)
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load - should get default values
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        # Should have default values from __post_init__
        assert config.multi_search_limits_config is not None
        assert config.multi_search_limits_config.multi_search_max_workers == 2
        assert config.multi_search_limits_config.multi_search_timeout_seconds == 30

    def test_config_roundtrip_preserves_multi_search_settings(self, tmp_path):
        """Multi_search settings should survive save/load roundtrip."""
        config_manager = ServerConfigManager(str(tmp_path))

        # Create, modify, save
        config = config_manager.create_default_config()
        config.multi_search_limits_config.multi_search_max_workers = 8
        config.multi_search_limits_config.scip_multi_timeout_seconds = 120
        config_manager.save_config(config)

        # Load and verify
        loaded_config = config_manager.load_config()
        assert loaded_config.multi_search_limits_config.multi_search_max_workers == 8
        assert loaded_config.multi_search_limits_config.scip_multi_timeout_seconds == 120


class TestServerConfigManagerMultiSearchValidation:
    """Test ServerConfigManager validation for MultiSearchLimitsConfig."""

    # ==========================================================================
    # Validation tests
    # ==========================================================================

    def test_validation_accepts_valid_worker_counts(self, tmp_path):
        """Valid worker counts should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Test various valid worker counts
        for workers in [1, 2, 5, 10, 20]:
            config.multi_search_limits_config.multi_search_max_workers = workers
            config.multi_search_limits_config.scip_multi_max_workers = workers
            # Should not raise
            config_manager.validate_config(config)

    def test_validation_accepts_valid_timeout_values(self, tmp_path):
        """Valid timeout values should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Test various valid timeout values
        for timeout in [5, 30, 60, 300, 600]:
            config.multi_search_limits_config.multi_search_timeout_seconds = timeout
            config.multi_search_limits_config.scip_multi_timeout_seconds = timeout
            # Should not raise
            config_manager.validate_config(config)

    def test_validation_rejects_zero_workers(self, tmp_path):
        """Zero workers should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.multi_search_max_workers = 0

        with pytest.raises(ValueError, match="multi_search_max_workers"):
            config_manager.validate_config(config)

    def test_validation_rejects_negative_workers(self, tmp_path):
        """Negative workers should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.scip_multi_max_workers = -1

        with pytest.raises(ValueError, match="scip_multi_max_workers"):
            config_manager.validate_config(config)

    def test_validation_rejects_zero_timeout(self, tmp_path):
        """Zero timeout should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.multi_search_timeout_seconds = 0

        with pytest.raises(ValueError, match="multi_search_timeout_seconds"):
            config_manager.validate_config(config)

    def test_validation_rejects_timeout_too_low(self, tmp_path):
        """Timeout below minimum (5 seconds) should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.scip_multi_timeout_seconds = 2

        with pytest.raises(ValueError, match="scip_multi_timeout_seconds"):
            config_manager.validate_config(config)

    def test_validation_rejects_workers_too_high(self, tmp_path):
        """Workers above maximum (50) should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.multi_search_max_workers = 100

        with pytest.raises(ValueError, match="multi_search_max_workers"):
            config_manager.validate_config(config)

    def test_validation_rejects_timeout_too_high(self, tmp_path):
        """Timeout above maximum (600 seconds) should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.multi_search_timeout_seconds = 1000

        with pytest.raises(ValueError, match="multi_search_timeout_seconds"):
            config_manager.validate_config(config)
