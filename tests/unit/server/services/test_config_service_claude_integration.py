"""
Unit tests for ConfigService.get_claude_integration_config() method.

Tests the new method added to fix Bug 4 in dependency map service wiring.
"""

import pytest
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


def test_get_claude_integration_config_returns_config(tmp_path):
    """Test that get_claude_integration_config returns the claude integration config."""
    # Arrange
    service = ConfigService(server_dir_path=str(tmp_path))
    config = service.get_config()

    # Act
    claude_config = service.get_claude_integration_config()

    # Assert
    assert claude_config is not None
    assert isinstance(claude_config, ClaudeIntegrationConfig)
    assert claude_config == config.claude_integration_config


def test_get_claude_integration_config_reflects_updates(tmp_path):
    """Test that get_claude_integration_config reflects config updates."""
    # Arrange
    service = ConfigService(server_dir_path=str(tmp_path))

    # Act - update a claude_cli setting
    service.update_setting("claude_cli", "max_concurrent_claude_cli", 5)
    claude_config = service.get_claude_integration_config()

    # Assert
    assert claude_config is not None
    assert claude_config.max_concurrent_claude_cli == 5


def test_get_claude_integration_config_loads_if_needed(tmp_path):
    """Test that get_claude_integration_config loads config if not already loaded."""
    # Arrange
    service = ConfigService(server_dir_path=str(tmp_path))
    # Don't call get_config() first - let get_claude_integration_config load it

    # Act
    claude_config = service.get_claude_integration_config()

    # Assert
    assert claude_config is not None
    assert isinstance(claude_config, ClaudeIntegrationConfig)
