"""
Unit tests for Story #359: Refinement configuration fields.

Tests cover Component 9:
- Default values for 3 new fields on ClaudeIntegrationConfig
- ConfigService exposes refinement settings in get_all_settings()
- ConfigService updates refinement settings via update_setting()

TDD RED PHASE: Tests written before production code exists.
"""

import pytest

from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig
from code_indexer.server.services.config_service import ConfigService


class TestDefaultRefinementConfig:
    """Default values for the 3 new refinement config fields."""

    def test_default_refinement_enabled_is_false(self):
        """refinement_enabled defaults to False (opt-in behavior)."""
        config = ClaudeIntegrationConfig()
        assert config.refinement_enabled is False

    def test_default_refinement_interval_hours_is_24(self):
        """refinement_interval_hours defaults to 24 hours."""
        config = ClaudeIntegrationConfig()
        assert config.refinement_interval_hours == 24

    def test_default_refinement_domains_per_run_is_3(self):
        """refinement_domains_per_run defaults to 3."""
        config = ClaudeIntegrationConfig()
        assert config.refinement_domains_per_run == 3

    def test_refinement_fields_independent_of_dependency_map(self):
        """Refinement fields are separate from existing dependency_map fields."""
        config = ClaudeIntegrationConfig(
            dependency_map_enabled=True,
            refinement_enabled=False,
        )
        assert config.dependency_map_enabled is True
        assert config.refinement_enabled is False

    def test_refinement_fields_settable_at_construction(self):
        """All 3 refinement fields can be set at construction time."""
        config = ClaudeIntegrationConfig(
            refinement_enabled=True,
            refinement_interval_hours=48,
            refinement_domains_per_run=5,
        )
        assert config.refinement_enabled is True
        assert config.refinement_interval_hours == 48
        assert config.refinement_domains_per_run == 5


class TestConfigServiceExposesRefinementSettings:
    """ConfigService.get_all_settings() exposes refinement fields under claude_cli."""

    def test_refinement_enabled_exposed_in_settings(self, tmp_path):
        """get_all_settings() includes refinement_enabled key."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "refinement_enabled" in settings["claude_cli"]

    def test_refinement_interval_hours_exposed_in_settings(self, tmp_path):
        """get_all_settings() includes refinement_interval_hours key."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "refinement_interval_hours" in settings["claude_cli"]

    def test_refinement_domains_per_run_exposed_in_settings(self, tmp_path):
        """get_all_settings() includes refinement_domains_per_run key."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "refinement_domains_per_run" in settings["claude_cli"]

    def test_refinement_enabled_default_value_in_settings(self, tmp_path):
        """get_all_settings() returns False for refinement_enabled by default."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert settings["claude_cli"]["refinement_enabled"] is False

    def test_refinement_interval_hours_default_in_settings(self, tmp_path):
        """get_all_settings() returns 24 for refinement_interval_hours by default."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert settings["claude_cli"]["refinement_interval_hours"] == 24

    def test_refinement_domains_per_run_default_in_settings(self, tmp_path):
        """get_all_settings() returns 3 for refinement_domains_per_run by default."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert settings["claude_cli"]["refinement_domains_per_run"] == 3


class TestConfigServiceUpdatesRefinementSettings:
    """ConfigService.update_setting() handles refinement fields correctly."""

    def test_update_refinement_enabled_true(self, tmp_path):
        """update_setting accepts true string for refinement_enabled."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "refinement_enabled", "true")
        config = service.get_claude_integration_config()
        assert config.refinement_enabled is True

    def test_update_refinement_enabled_false(self, tmp_path):
        """update_setting accepts false string for refinement_enabled."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "refinement_enabled", True)
        service.update_setting("claude_cli", "refinement_enabled", "false")
        config = service.get_claude_integration_config()
        assert config.refinement_enabled is False

    def test_update_refinement_interval_hours(self, tmp_path):
        """update_setting updates refinement_interval_hours to specified value."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "refinement_interval_hours", 48)
        config = service.get_claude_integration_config()
        assert config.refinement_interval_hours == 48

    def test_update_refinement_interval_hours_clamped_to_min(self, tmp_path):
        """refinement_interval_hours is clamped to minimum value of 1."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "refinement_interval_hours", 0)
        config = service.get_claude_integration_config()
        assert config.refinement_interval_hours == 1

    def test_update_refinement_domains_per_run(self, tmp_path):
        """update_setting updates refinement_domains_per_run to specified value."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "refinement_domains_per_run", 5)
        config = service.get_claude_integration_config()
        assert config.refinement_domains_per_run == 5

    def test_update_refinement_domains_per_run_clamped_to_min(self, tmp_path):
        """refinement_domains_per_run is clamped to minimum value of 1."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting("claude_cli", "refinement_domains_per_run", 0)
        config = service.get_claude_integration_config()
        assert config.refinement_domains_per_run == 1

    def test_unknown_refinement_setting_rejected(self, tmp_path):
        """Unknown claude_cli settings raise ValueError (existing behavior preserved)."""
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(ValueError, match="Unknown claude_cli setting"):
            service.update_setting("claude_cli", "refinement_unknown_field", "value")
