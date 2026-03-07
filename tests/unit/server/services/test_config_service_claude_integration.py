"""
Unit tests for ConfigService.get_claude_integration_config() method.

Tests the new method added to fix Bug 4 in dependency map service wiring.
Also tests that get_all_settings() includes subscription-related fields (Bug: subscription config display).
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


class TestGetAllSettingsSubscriptionFields:
    """Tests that get_all_settings() includes subscription-related fields in claude_cli dict.

    These fields were missing, causing the config UI to always show 'API Key' mode
    even when the backend was configured for subscription mode.
    """

    def test_get_all_settings_includes_claude_auth_mode(self, tmp_path):
        """Test that get_all_settings() includes claude_auth_mode in claude_cli dict."""
        service = ConfigService(server_dir_path=str(tmp_path))

        settings = service.get_all_settings()

        assert "claude_cli" in settings
        assert "claude_auth_mode" in settings["claude_cli"], (
            "claude_auth_mode missing from claude_cli dict — "
            "template cannot display correct auth mode"
        )

    def test_get_all_settings_claude_auth_mode_default_is_api_key(self, tmp_path):
        """Test that default claude_auth_mode is 'api_key'."""
        service = ConfigService(server_dir_path=str(tmp_path))

        settings = service.get_all_settings()

        assert settings["claude_cli"]["claude_auth_mode"] == "api_key"

    def test_get_all_settings_claude_auth_mode_reflects_subscription(self, tmp_path):
        """Test that when claude_auth_mode is 'subscription', it appears as 'subscription'."""
        service = ConfigService(server_dir_path=str(tmp_path))
        # Set auth mode to subscription
        service.update_setting("claude_cli", "claude_auth_mode", "subscription")

        settings = service.get_all_settings()

        assert settings["claude_cli"]["claude_auth_mode"] == "subscription"

    def test_get_all_settings_includes_llm_creds_provider_url(self, tmp_path):
        """Test that get_all_settings() includes llm_creds_provider_url in claude_cli dict."""
        service = ConfigService(server_dir_path=str(tmp_path))

        settings = service.get_all_settings()

        assert "claude_cli" in settings
        assert "llm_creds_provider_url" in settings["claude_cli"], (
            "llm_creds_provider_url missing from claude_cli dict"
        )

    def test_get_all_settings_llm_creds_provider_url_reflects_value(self, tmp_path):
        """Test that llm_creds_provider_url reflects the configured value."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting(
            "claude_cli", "llm_creds_provider_url", "https://creds.example.com"
        )

        settings = service.get_all_settings()

        assert settings["claude_cli"]["llm_creds_provider_url"] == "https://creds.example.com"

    def test_get_all_settings_includes_llm_creds_provider_api_key_masked(self, tmp_path):
        """Test that llm_creds_provider_api_key is present and masked in get_all_settings()."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting(
            "claude_cli", "llm_creds_provider_api_key", "sk-secret-key-12345"
        )

        settings = service.get_all_settings()

        assert "llm_creds_provider_api_key" in settings["claude_cli"], (
            "llm_creds_provider_api_key missing from claude_cli dict"
        )
        api_key_value = settings["claude_cli"]["llm_creds_provider_api_key"]
        assert api_key_value is not None
        # Must be masked - should not contain the full key
        assert "sk-secret-key-12345" != api_key_value, (
            "llm_creds_provider_api_key must be masked in get_all_settings() output"
        )
        # Should end with *** masking
        assert api_key_value.endswith("***"), (
            f"Expected masked key ending with ***, got: {api_key_value}"
        )

    def test_get_all_settings_llm_creds_provider_api_key_none_when_empty(self, tmp_path):
        """Test that llm_creds_provider_api_key is None when not configured."""
        service = ConfigService(server_dir_path=str(tmp_path))
        # Default config has empty llm_creds_provider_api_key

        settings = service.get_all_settings()

        assert "llm_creds_provider_api_key" in settings["claude_cli"]
        # Empty/None key should return None (not masked empty string)
        assert settings["claude_cli"]["llm_creds_provider_api_key"] is None

    def test_get_all_settings_includes_llm_creds_provider_consumer_id(self, tmp_path):
        """Test that get_all_settings() includes llm_creds_provider_consumer_id in claude_cli."""
        service = ConfigService(server_dir_path=str(tmp_path))

        settings = service.get_all_settings()

        assert "claude_cli" in settings
        assert "llm_creds_provider_consumer_id" in settings["claude_cli"], (
            "llm_creds_provider_consumer_id missing from claude_cli dict"
        )

    def test_get_all_settings_llm_creds_provider_consumer_id_reflects_value(self, tmp_path):
        """Test that llm_creds_provider_consumer_id reflects the configured value."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.update_setting(
            "claude_cli", "llm_creds_provider_consumer_id", "my-custom-consumer"
        )

        settings = service.get_all_settings()

        assert (
            settings["claude_cli"]["llm_creds_provider_consumer_id"] == "my-custom-consumer"
        )

    def test_update_setting_claude_auth_mode_rejects_invalid_value(self, tmp_path):
        """update_setting rejects invalid claude_auth_mode values."""
        service = ConfigService(server_dir_path=str(tmp_path))
        with pytest.raises(ValueError, match="Invalid claude_auth_mode"):
            service.update_setting("claude_cli", "claude_auth_mode", "oauth2")
