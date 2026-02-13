"""
Unit tests for ClaudeIntegrationConfig.description_refresh_enabled field (Story #190).

Tests that the new description_refresh_enabled field defaults to False and can be serialized/deserialized correctly.
"""

import json
from pathlib import Path

import pytest

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ClaudeIntegrationConfig,
)


def test_description_refresh_enabled_defaults_to_false():
    """Test description_refresh_enabled defaults to False in new configs."""
    config = ClaudeIntegrationConfig()
    assert config.description_refresh_enabled is False


def test_description_refresh_enabled_saves_and_loads(tmp_path: Path):
    """Test description_refresh_enabled persists correctly through save/load cycle."""
    config_manager = ServerConfigManager(str(tmp_path))
    config = config_manager.create_default_config()

    # Set to True
    assert config.claude_integration_config is not None
    config.claude_integration_config.description_refresh_enabled = True

    # Save and load
    config_manager.save_config(config)
    loaded_config = config_manager.load_config()

    assert loaded_config is not None
    assert loaded_config.claude_integration_config is not None
    assert loaded_config.claude_integration_config.description_refresh_enabled is True


def test_description_refresh_enabled_missing_from_old_config(tmp_path: Path):
    """Test backward compatibility when description_refresh_enabled is missing from old configs."""
    # Create old config without description_refresh_enabled
    config_data = {
        "server_dir": str(tmp_path),
        "claude_integration_config": {
            "anthropic_api_key": "test-key",
            "description_refresh_interval_hours": 24,
        },
    }

    config_file = tmp_path / "config.json"
    with open(config_file, "w") as f:
        json.dump(config_data, f)

    # Load and verify field defaults to False
    config_manager = ServerConfigManager(str(tmp_path))
    loaded_config = config_manager.load_config()

    assert loaded_config is not None
    assert loaded_config.claude_integration_config is not None
    assert loaded_config.claude_integration_config.description_refresh_enabled is False


def test_description_refresh_enabled_explicit_false(tmp_path: Path):
    """Test description_refresh_enabled can be explicitly set to False."""
    config_manager = ServerConfigManager(str(tmp_path))
    config = config_manager.create_default_config()

    # Explicitly set to False
    assert config.claude_integration_config is not None
    config.claude_integration_config.description_refresh_enabled = False

    # Save and load
    config_manager.save_config(config)
    loaded_config = config_manager.load_config()

    assert loaded_config is not None
    assert loaded_config.claude_integration_config is not None
    assert loaded_config.claude_integration_config.description_refresh_enabled is False
