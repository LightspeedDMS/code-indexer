"""
Unit tests for BackgroundJobsConfig configuration (Story #26).

Tests the configuration for BackgroundJobManager concurrent job limiting
that is now configurable via Web UI Configuration system.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import pytest

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    BackgroundJobsConfig,
)


class TestBackgroundJobsConfigDataclass:
    """Test suite for BackgroundJobsConfig dataclass (Story #26)."""

    # ==========================================================================
    # AC1: Default value for max_concurrent_background_jobs (default: 5)
    # ==========================================================================

    def test_default_max_concurrent_background_jobs(self):
        """AC1: Default max_concurrent_background_jobs should be 5."""
        config = BackgroundJobsConfig()
        assert config.max_concurrent_background_jobs == 5

    # ==========================================================================
    # Dataclass field tests
    # ==========================================================================

    def test_custom_values_initialization(self):
        """Custom values should be accepted during initialization."""
        config = BackgroundJobsConfig(
            max_concurrent_background_jobs=10,
        )
        assert config.max_concurrent_background_jobs == 10

    def test_minimum_value_one(self):
        """Value of 1 should be allowed (single job at a time)."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=1)
        assert config.max_concurrent_background_jobs == 1


class TestServerConfigBackgroundJobsIntegration:
    """Test ServerConfig integration with BackgroundJobsConfig."""

    # ==========================================================================
    # ServerConfig should have background_jobs_config field
    # ==========================================================================

    def test_server_config_has_background_jobs_config_field(self):
        """ServerConfig should have background_jobs_config field."""
        config = ServerConfig(server_dir="/tmp/test")
        assert hasattr(config, "background_jobs_config")

    def test_server_config_initializes_background_jobs_config(self):
        """ServerConfig should auto-initialize background_jobs_config in __post_init__."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.background_jobs_config is not None
        assert isinstance(config.background_jobs_config, BackgroundJobsConfig)

    def test_server_config_background_jobs_defaults(self):
        """ServerConfig should have default BackgroundJobsConfig values."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.background_jobs_config.max_concurrent_background_jobs == 5


class TestServerConfigManagerBackgroundJobsPersistence:
    """Test ServerConfigManager save/load for BackgroundJobsConfig."""

    # ==========================================================================
    # Config save/load roundtrip
    # ==========================================================================

    def test_default_config_has_background_jobs(self, tmp_path):
        """AC1: Fresh installation should have default BackgroundJobsConfig."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        assert config.background_jobs_config is not None
        assert config.background_jobs_config.max_concurrent_background_jobs == 5

    def test_custom_background_jobs_config_saves_to_file(self, tmp_path):
        """Custom background_jobs settings should persist when saved to config file."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Set custom values
        config.background_jobs_config.max_concurrent_background_jobs = 10
        config_manager.save_config(config)

        # Verify it was saved to JSON
        config_file = tmp_path / "config.json"
        with open(config_file) as f:
            saved_config = json.load(f)

        assert "background_jobs_config" in saved_config
        assert saved_config["background_jobs_config"]["max_concurrent_background_jobs"] == 10

    def test_custom_background_jobs_config_loads_from_file(self, tmp_path):
        """Custom background_jobs settings should load correctly from config file."""
        # Create config file with custom background_jobs settings
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "background_jobs_config": {
                "max_concurrent_background_jobs": 8,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load and verify
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config.background_jobs_config.max_concurrent_background_jobs == 8

    def test_config_without_background_jobs_gets_defaults(self, tmp_path):
        """Old config without background_jobs_config should get defaults."""
        # Create config file without background_jobs_config (simulates old config)
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
        assert config.background_jobs_config is not None
        assert config.background_jobs_config.max_concurrent_background_jobs == 5

    def test_config_roundtrip_preserves_background_jobs_settings(self, tmp_path):
        """Background_jobs settings should survive save/load roundtrip."""
        config_manager = ServerConfigManager(str(tmp_path))

        # Create, modify, save
        config = config_manager.create_default_config()
        config.background_jobs_config.max_concurrent_background_jobs = 12
        config_manager.save_config(config)

        # Load and verify
        loaded_config = config_manager.load_config()
        assert loaded_config.background_jobs_config.max_concurrent_background_jobs == 12


class TestServerConfigManagerBackgroundJobsValidation:
    """Test ServerConfigManager validation for BackgroundJobsConfig."""

    # ==========================================================================
    # Validation tests
    # ==========================================================================

    def test_validation_accepts_valid_job_counts(self, tmp_path):
        """Valid job counts should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Test various valid job counts
        for jobs in [1, 2, 5, 10, 20, 50]:
            config.background_jobs_config.max_concurrent_background_jobs = jobs
            # Should not raise
            config_manager.validate_config(config)

    def test_validation_rejects_zero_jobs(self, tmp_path):
        """Zero concurrent jobs should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.background_jobs_config.max_concurrent_background_jobs = 0

        with pytest.raises(ValueError, match="max_concurrent_background_jobs"):
            config_manager.validate_config(config)

    def test_validation_rejects_negative_jobs(self, tmp_path):
        """Negative concurrent jobs should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.background_jobs_config.max_concurrent_background_jobs = -1

        with pytest.raises(ValueError, match="max_concurrent_background_jobs"):
            config_manager.validate_config(config)

    def test_validation_rejects_jobs_too_high(self, tmp_path):
        """Jobs above maximum (100) should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.background_jobs_config.max_concurrent_background_jobs = 200

        with pytest.raises(ValueError, match="max_concurrent_background_jobs"):
            config_manager.validate_config(config)
