"""
Unit tests for SubprocessExecutor max_workers configuration (Story #27).

Tests the configuration for SubprocessExecutor concurrent worker limiting
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


class TestBackgroundJobsConfigSubprocessExecutorField:
    """Test suite for subprocess_max_workers field in BackgroundJobsConfig (Story #27)."""

    # ==========================================================================
    # AC1: Default value for subprocess_max_workers (default: 2 per resource audit)
    # ==========================================================================

    def test_default_subprocess_max_workers(self):
        """AC1: Default subprocess_max_workers should be 2 per resource audit."""
        config = BackgroundJobsConfig()
        assert config.subprocess_max_workers == 2

    # ==========================================================================
    # Dataclass field tests
    # ==========================================================================

    def test_custom_subprocess_max_workers_initialization(self):
        """Custom subprocess_max_workers should be accepted during initialization."""
        config = BackgroundJobsConfig(
            subprocess_max_workers=4,
        )
        assert config.subprocess_max_workers == 4

    def test_subprocess_max_workers_minimum_value_one(self):
        """Value of 1 should be allowed (single worker)."""
        config = BackgroundJobsConfig(subprocess_max_workers=1)
        assert config.subprocess_max_workers == 1

    def test_subprocess_max_workers_coexists_with_max_concurrent_background_jobs(self):
        """Both subprocess_max_workers and max_concurrent_background_jobs should work together."""
        config = BackgroundJobsConfig(
            max_concurrent_background_jobs=10,
            subprocess_max_workers=4,
        )
        assert config.max_concurrent_background_jobs == 10
        assert config.subprocess_max_workers == 4


class TestServerConfigSubprocessExecutorIntegration:
    """Test ServerConfig integration with subprocess_max_workers (Story #27)."""

    # ==========================================================================
    # ServerConfig should expose subprocess_max_workers via background_jobs_config
    # ==========================================================================

    def test_server_config_background_jobs_has_subprocess_max_workers(self):
        """ServerConfig.background_jobs_config should have subprocess_max_workers."""
        config = ServerConfig(server_dir="/tmp/test")
        assert hasattr(config.background_jobs_config, "subprocess_max_workers")

    def test_server_config_subprocess_max_workers_default(self):
        """ServerConfig should have default subprocess_max_workers of 2."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.background_jobs_config.subprocess_max_workers == 2


class TestServerConfigManagerSubprocessExecutorPersistence:
    """Test ServerConfigManager save/load for subprocess_max_workers (Story #27)."""

    # ==========================================================================
    # Config save/load roundtrip
    # ==========================================================================

    def test_default_config_has_subprocess_max_workers(self, tmp_path):
        """AC1: Fresh installation should have default subprocess_max_workers of 2."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        assert config.background_jobs_config is not None
        assert config.background_jobs_config.subprocess_max_workers == 2

    def test_custom_subprocess_max_workers_saves_to_file(self, tmp_path):
        """Custom subprocess_max_workers should persist when saved to config file."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Set custom value
        config.background_jobs_config.subprocess_max_workers = 8
        config_manager.save_config(config)

        # Verify it was saved to JSON
        config_file = tmp_path / "config.json"
        with open(config_file) as f:
            saved_config = json.load(f)

        assert "background_jobs_config" in saved_config
        assert saved_config["background_jobs_config"]["subprocess_max_workers"] == 8

    def test_custom_subprocess_max_workers_loads_from_file(self, tmp_path):
        """Custom subprocess_max_workers should load correctly from config file."""
        # Create config file with custom subprocess_max_workers
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "background_jobs_config": {
                "max_concurrent_background_jobs": 5,
                "subprocess_max_workers": 6,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load and verify
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config.background_jobs_config.subprocess_max_workers == 6

    def test_config_without_subprocess_max_workers_gets_default(self, tmp_path):
        """Old config without subprocess_max_workers should get default of 2."""
        # Create config file without subprocess_max_workers (simulates old config)
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "background_jobs_config": {
                "max_concurrent_background_jobs": 5,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load - should get default value
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        # Should have default value
        assert config.background_jobs_config.subprocess_max_workers == 2

    def test_config_roundtrip_preserves_subprocess_max_workers(self, tmp_path):
        """subprocess_max_workers should survive save/load roundtrip."""
        config_manager = ServerConfigManager(str(tmp_path))

        # Create, modify, save
        config = config_manager.create_default_config()
        config.background_jobs_config.subprocess_max_workers = 16
        config_manager.save_config(config)

        # Load and verify
        loaded_config = config_manager.load_config()
        assert loaded_config.background_jobs_config.subprocess_max_workers == 16


class TestServerConfigManagerSubprocessExecutorValidation:
    """Test ServerConfigManager validation for subprocess_max_workers (Story #27)."""

    # ==========================================================================
    # Validation tests - range 1-50
    # ==========================================================================

    def test_validation_accepts_valid_worker_counts(self, tmp_path):
        """Valid worker counts should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Test various valid worker counts
        for workers in [1, 2, 4, 8, 16, 32, 50]:
            config.background_jobs_config.subprocess_max_workers = workers
            # Should not raise
            config_manager.validate_config(config)

    def test_validation_rejects_zero_workers(self, tmp_path):
        """Zero workers should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.background_jobs_config.subprocess_max_workers = 0

        with pytest.raises(ValueError, match="subprocess_max_workers"):
            config_manager.validate_config(config)

    def test_validation_rejects_negative_workers(self, tmp_path):
        """Negative workers should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.background_jobs_config.subprocess_max_workers = -1

        with pytest.raises(ValueError, match="subprocess_max_workers"):
            config_manager.validate_config(config)

    def test_validation_rejects_workers_too_high(self, tmp_path):
        """Workers above maximum (50) should fail validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.background_jobs_config.subprocess_max_workers = 100

        with pytest.raises(ValueError, match="subprocess_max_workers"):
            config_manager.validate_config(config)
