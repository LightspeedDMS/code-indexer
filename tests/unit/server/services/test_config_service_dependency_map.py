"""
Unit tests for ConfigService dependency map configuration field clamping.

Tests that numeric dependency map fields are properly clamped to their min/max ranges.
"""

import pytest
from code_indexer.server.services.config_service import ConfigService


def test_dependency_map_interval_hours_clamped_to_min(tmp_path):
    """Test that dependency_map_interval_hours is clamped to minimum value of 1."""
    service = ConfigService(server_dir_path=str(tmp_path))

    # Try to set below minimum
    service.update_setting("claude_cli", "dependency_map_interval_hours", 0)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_interval_hours == 1


def test_dependency_map_interval_hours_clamped_to_max(tmp_path):
    """Test that dependency_map_interval_hours is clamped to maximum value of 8760."""
    service = ConfigService(server_dir_path=str(tmp_path))

    # Try to set above maximum
    service.update_setting("claude_cli", "dependency_map_interval_hours", 10000)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_interval_hours == 8760


def test_dependency_map_interval_hours_accepts_valid_value(tmp_path):
    """Test that dependency_map_interval_hours accepts valid values."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_interval_hours", 168)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_interval_hours == 168


def test_dependency_map_pass_timeout_clamped_to_min(tmp_path):
    """Test that dependency_map_pass_timeout_seconds is clamped to minimum value of 60."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass_timeout_seconds", 30)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass_timeout_seconds == 60


def test_dependency_map_pass_timeout_clamped_to_max(tmp_path):
    """Test that dependency_map_pass_timeout_seconds is clamped to maximum value of 3600."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass_timeout_seconds", 5000)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass_timeout_seconds == 3600


def test_dependency_map_pass_timeout_accepts_valid_value(tmp_path):
    """Test that dependency_map_pass_timeout_seconds accepts valid values."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass_timeout_seconds", 600)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass_timeout_seconds == 600


def test_dependency_map_pass1_max_turns_clamped_to_min(tmp_path):
    """Test that dependency_map_pass1_max_turns is clamped to minimum value of 5."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass1_max_turns", 1)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass1_max_turns == 5


def test_dependency_map_pass1_max_turns_clamped_to_max(tmp_path):
    """Test that dependency_map_pass1_max_turns is clamped to maximum value of 200."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass1_max_turns", 300)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass1_max_turns == 200


def test_dependency_map_pass2_max_turns_clamped_to_min(tmp_path):
    """Test that dependency_map_pass2_max_turns is clamped to minimum value of 5."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass2_max_turns", 2)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass2_max_turns == 5


def test_dependency_map_pass2_max_turns_clamped_to_max(tmp_path):
    """Test that dependency_map_pass2_max_turns is clamped to maximum value of 200."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass2_max_turns", 250)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass2_max_turns == 200


def test_dependency_map_pass3_max_turns_clamped_to_min(tmp_path):
    """Test that dependency_map_pass3_max_turns is clamped to minimum value of 5."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass3_max_turns", 3)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass3_max_turns == 5


def test_dependency_map_pass3_max_turns_clamped_to_max(tmp_path):
    """Test that dependency_map_pass3_max_turns is clamped to maximum value of 200."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass3_max_turns", 999)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass3_max_turns == 200


def test_dependency_map_delta_max_turns_clamped_to_min(tmp_path):
    """Test that dependency_map_delta_max_turns is clamped to minimum value of 5."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_delta_max_turns", 0)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_delta_max_turns == 5


def test_dependency_map_delta_max_turns_clamped_to_max(tmp_path):
    """Test that dependency_map_delta_max_turns is clamped to maximum value of 200."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_delta_max_turns", 500)
    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_delta_max_turns == 200


def test_all_dependency_map_max_turns_accept_valid_values(tmp_path):
    """Test that all max_turns fields accept valid values in range."""
    service = ConfigService(server_dir_path=str(tmp_path))

    service.update_setting("claude_cli", "dependency_map_pass1_max_turns", 50)
    service.update_setting("claude_cli", "dependency_map_pass2_max_turns", 60)
    service.update_setting("claude_cli", "dependency_map_pass3_max_turns", 30)
    service.update_setting("claude_cli", "dependency_map_delta_max_turns", 30)

    claude_config = service.get_claude_integration_config()

    assert claude_config.dependency_map_pass1_max_turns == 50
    assert claude_config.dependency_map_pass2_max_turns == 60
    assert claude_config.dependency_map_pass3_max_turns == 30
    assert claude_config.dependency_map_delta_max_turns == 30
