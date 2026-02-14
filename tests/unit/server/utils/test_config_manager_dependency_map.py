"""
Unit tests for ClaudeIntegrationConfig dependency map fields (Story #192).

Tests the 7 new configuration fields:
- dependency_map_enabled
- dependency_map_interval_hours
- dependency_map_pass_timeout_seconds
- dependency_map_pass1_max_turns
- dependency_map_pass2_max_turns
- dependency_map_pass3_max_turns
- dependency_map_delta_max_turns
"""

import json
import pytest
from pathlib import Path

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ClaudeIntegrationConfig,
)


class TestDependencyMapConfigDefaults:
    """Test default values for dependency map config fields (AC6)."""

    def test_dependency_map_defaults(self, tmp_path):
        """Test that dependency map config fields have correct defaults."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        assert config.claude_integration_config is not None
        claude_config = config.claude_integration_config

        # Verify defaults
        assert claude_config.dependency_map_enabled is False
        assert claude_config.dependency_map_interval_hours == 168  # 1 week
        assert claude_config.dependency_map_pass_timeout_seconds == 600  # 10 minutes
        assert claude_config.dependency_map_pass1_max_turns == 50
        assert claude_config.dependency_map_pass2_max_turns == 60
        assert claude_config.dependency_map_pass3_max_turns == 30
        assert claude_config.dependency_map_delta_max_turns == 30


class TestDependencyMapConfigPersistence:
    """Test config persistence for dependency map fields."""

    def test_save_and_load_dependency_map_config(self, tmp_path):
        """Test that dependency map config fields are saved and loaded correctly."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Modify dependency map settings
        config.claude_integration_config.dependency_map_enabled = True
        config.claude_integration_config.dependency_map_interval_hours = 72
        config.claude_integration_config.dependency_map_pass_timeout_seconds = 900
        config.claude_integration_config.dependency_map_pass1_max_turns = 40
        config.claude_integration_config.dependency_map_pass2_max_turns = 70
        config.claude_integration_config.dependency_map_pass3_max_turns = 35
        config.claude_integration_config.dependency_map_delta_max_turns = 25

        # Save config
        config_manager.save_config(config)

        # Load config
        loaded_config = config_manager.load_config()

        assert loaded_config is not None
        assert loaded_config.claude_integration_config is not None
        claude_config = loaded_config.claude_integration_config

        # Verify loaded values
        assert claude_config.dependency_map_enabled is True
        assert claude_config.dependency_map_interval_hours == 72
        assert claude_config.dependency_map_pass_timeout_seconds == 900
        assert claude_config.dependency_map_pass1_max_turns == 40
        assert claude_config.dependency_map_pass2_max_turns == 70
        assert claude_config.dependency_map_pass3_max_turns == 35
        assert claude_config.dependency_map_delta_max_turns == 25

    def test_load_config_with_missing_dependency_map_fields(self, tmp_path):
        """Test that loading config without dependency map fields uses defaults."""
        config_manager = ServerConfigManager(str(tmp_path))

        # Create config file without dependency map fields (simulate old config)
        config_file = tmp_path / "config.json"
        config_data = {
            "server_dir": str(tmp_path),
            "claude_integration_config": {
                "anthropic_api_key": None,
                "description_refresh_enabled": False,
            },
        }
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load config
        loaded_config = config_manager.load_config()

        assert loaded_config is not None
        assert loaded_config.claude_integration_config is not None
        claude_config = loaded_config.claude_integration_config

        # Verify defaults are applied
        assert claude_config.dependency_map_enabled is False
        assert claude_config.dependency_map_interval_hours == 168
        assert claude_config.dependency_map_pass_timeout_seconds == 600
        assert claude_config.dependency_map_pass1_max_turns == 50
        assert claude_config.dependency_map_pass2_max_turns == 60
        assert claude_config.dependency_map_pass3_max_turns == 30
        assert claude_config.dependency_map_delta_max_turns == 30


class TestClaudeIntegrationConfigDataclass:
    """Test ClaudeIntegrationConfig dataclass directly."""

    def test_create_claude_integration_config_with_dependency_map(self):
        """Test creating ClaudeIntegrationConfig with dependency map fields."""
        config = ClaudeIntegrationConfig(
            dependency_map_enabled=True,
            dependency_map_interval_hours=96,
            dependency_map_pass_timeout_seconds=1200,
            dependency_map_pass1_max_turns=45,
            dependency_map_pass2_max_turns=65,
            dependency_map_pass3_max_turns=32,
            dependency_map_delta_max_turns=28,
        )

        assert config.dependency_map_enabled is True
        assert config.dependency_map_interval_hours == 96
        assert config.dependency_map_pass_timeout_seconds == 1200
        assert config.dependency_map_pass1_max_turns == 45
        assert config.dependency_map_pass2_max_turns == 65
        assert config.dependency_map_pass3_max_turns == 32
        assert config.dependency_map_delta_max_turns == 28
