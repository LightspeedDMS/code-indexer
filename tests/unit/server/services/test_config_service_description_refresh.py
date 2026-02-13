"""
Unit tests for ConfigService description_refresh_enabled setting (Story #190).

Tests verify that description_refresh_enabled can be read and written via config_service.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceDescriptionRefreshEnabled:
    """Test ConfigService description_refresh_enabled setting."""

    def test_get_all_settings_includes_description_refresh_enabled(self, tmp_path):
        """Test that description_refresh_enabled is included in get_all_settings()."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert "claude_cli" in settings
        assert "description_refresh_enabled" in settings["claude_cli"]

    def test_description_refresh_enabled_default_value(self, tmp_path):
        """Test that description_refresh_enabled has default value from config."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        # Default value from ClaudeIntegrationConfig
        assert isinstance(settings["claude_cli"]["description_refresh_enabled"], bool)

    def test_update_description_refresh_enabled_with_true(self, tmp_path):
        """Test updating description_refresh_enabled to True."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Update to True
        service.update_setting("claude_cli", "description_refresh_enabled", True)

        # Verify updated
        settings = service.get_all_settings()
        assert settings["claude_cli"]["description_refresh_enabled"] is True

    def test_update_description_refresh_enabled_with_false(self, tmp_path):
        """Test updating description_refresh_enabled to False."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Update to False
        service.update_setting("claude_cli", "description_refresh_enabled", False)

        # Verify updated
        settings = service.get_all_settings()
        assert settings["claude_cli"]["description_refresh_enabled"] is False

    def test_update_description_refresh_enabled_with_string_true(self, tmp_path):
        """Test updating description_refresh_enabled with string 'true'."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Update with string "true"
        service.update_setting("claude_cli", "description_refresh_enabled", "true")

        # Verify converted to boolean
        settings = service.get_all_settings()
        assert settings["claude_cli"]["description_refresh_enabled"] is True

    def test_update_description_refresh_enabled_persists(self, tmp_path):
        """Test that description_refresh_enabled updates persist to disk."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Update setting
        service.update_setting("claude_cli", "description_refresh_enabled", True)

        # Create new service instance and verify persistence
        new_service = ConfigService(server_dir_path=str(tmp_path))
        new_service.load_config()
        settings = new_service.get_all_settings()

        assert settings["claude_cli"]["description_refresh_enabled"] is True

    def test_update_unknown_claude_cli_setting_raises_error(self, tmp_path):
        """Test that updating unknown claude_cli setting raises ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="Unknown claude_cli setting"):
            service.update_setting("claude_cli", "unknown_setting", "value")
