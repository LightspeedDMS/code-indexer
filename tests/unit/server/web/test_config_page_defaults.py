"""
Test config page default values for claude_cli.

Tests Bug #153 fix: _get_current_config() providing defaults when claude_cli is empty dict.
"""

import pytest
from unittest.mock import Mock, patch

from code_indexer.server.web.routes import _get_current_config


class TestConfigPageDefaults:
    """Test _get_current_config() provides defaults for missing or empty configs."""

    def _create_base_settings(self, claude_cli_value=None):
        """Create base settings dict with optional claude_cli override."""
        # Minimal settings dict - _get_current_config provides defaults for missing sections
        settings = {
            "server": {"host": "0.0.0.0", "port": 8000},
            "cache": {"ttl_seconds": 300},
            "timeouts": {"default": 30},
            "password_security": {"min_length": 8},
        }
        if claude_cli_value is not None:
            settings["claude_cli"] = claude_cli_value
        return settings

    def test_claude_cli_missing_returns_defaults(self):
        """Test that missing claude_cli config returns defaults."""
        # Given: Settings without claude_cli key
        settings = self._create_base_settings()
        # claude_cli is intentionally missing

        # When: Getting current config
        # Patch at source module since get_config_service is imported via 'from' statement
        with patch("code_indexer.server.services.config_service.get_config_service") as mock_service:
            mock_config_service = Mock()
            mock_config_service.get_all_settings.return_value = settings
            mock_service.return_value = mock_config_service

            config = _get_current_config()

        # Then: Should have claude_cli with defaults
        assert "claude_cli" in config
        assert config["claude_cli"]["max_concurrent_claude_cli"] == 3
        assert config["claude_cli"]["description_refresh_interval_hours"] == 24
        assert config["claude_cli"]["research_assistant_timeout_seconds"] == 300

    def test_claude_cli_empty_dict_returns_defaults(self):
        """Test that empty claude_cli dict returns defaults."""
        # Given: Settings with empty claude_cli dict
        settings = self._create_base_settings(claude_cli_value={})

        # When: Getting current config
        with patch("code_indexer.server.services.config_service.get_config_service") as mock_service:
            mock_config_service = Mock()
            mock_config_service.get_all_settings.return_value = settings
            mock_service.return_value = mock_config_service

            config = _get_current_config()

        # Then: Should have claude_cli with defaults
        assert "claude_cli" in config
        assert config["claude_cli"]["max_concurrent_claude_cli"] == 3
        assert config["claude_cli"]["description_refresh_interval_hours"] == 24
        assert config["claude_cli"]["research_assistant_timeout_seconds"] == 300

    def test_claude_cli_none_returns_defaults(self):
        """Test that None claude_cli returns defaults."""
        # Given: Settings with None claude_cli
        settings = self._create_base_settings(claude_cli_value=None)

        # When: Getting current config
        with patch("code_indexer.server.services.config_service.get_config_service") as mock_service:
            mock_config_service = Mock()
            mock_config_service.get_all_settings.return_value = settings
            mock_service.return_value = mock_config_service

            config = _get_current_config()

        # Then: Should have claude_cli with defaults
        assert "claude_cli" in config
        assert config["claude_cli"]["max_concurrent_claude_cli"] == 3
        assert config["claude_cli"]["description_refresh_interval_hours"] == 24
        assert config["claude_cli"]["research_assistant_timeout_seconds"] == 300

    def test_claude_cli_partial_config_preserves_values(self):
        """Test that partial claude_cli config preserves existing values and fills missing."""
        # Given: Settings with partial claude_cli config
        settings = self._create_base_settings(claude_cli_value={
            "max_concurrent_claude_cli": 5,
            # Other fields missing
        })

        # When: Getting current config
        with patch("code_indexer.server.services.config_service.get_config_service") as mock_service:
            mock_config_service = Mock()
            mock_config_service.get_all_settings.return_value = settings
            mock_service.return_value = mock_config_service

            config = _get_current_config()

        # Then: Should preserve existing value and fill defaults for missing
        assert config["claude_cli"]["max_concurrent_claude_cli"] == 5
        assert config["claude_cli"]["description_refresh_interval_hours"] == 24
        assert config["claude_cli"]["research_assistant_timeout_seconds"] == 300

    def test_claude_cli_with_api_keys_works(self):
        """Test that claude_cli with API keys works correctly."""
        # Given: Settings with complete claude_cli config including API keys
        settings = self._create_base_settings(claude_cli_value={
            "max_concurrent_claude_cli": 3,
            "description_refresh_interval_hours": 24,
            "research_assistant_timeout_seconds": 300,
            "anthropic_api_key": "sk-ant-test123",
            "voyageai_api_key": "pa-test456",
        })

        # When: Getting current config
        with patch("code_indexer.server.services.config_service.get_config_service") as mock_service:
            mock_config_service = Mock()
            mock_config_service.get_all_settings.return_value = settings
            mock_service.return_value = mock_config_service

            config = _get_current_config()

        # Then: Should have all values including API keys
        assert config["claude_cli"]["max_concurrent_claude_cli"] == 3
        assert config["claude_cli"]["description_refresh_interval_hours"] == 24
        assert config["claude_cli"]["research_assistant_timeout_seconds"] == 300
        assert config["claude_cli"]["anthropic_api_key"] == "sk-ant-test123"
        assert config["claude_cli"]["voyageai_api_key"] == "pa-test456"

    def test_provider_api_keys_with_empty_claude_cli(self):
        """Test that provider_api_keys works even when claude_cli is empty."""
        # Given: Settings with empty claude_cli
        settings = self._create_base_settings(claude_cli_value={})

        # When: Getting current config
        with patch("code_indexer.server.services.config_service.get_config_service") as mock_service:
            mock_config_service = Mock()
            mock_config_service.get_all_settings.return_value = settings
            mock_service.return_value = mock_config_service

            config = _get_current_config()

        # Then: provider_api_keys should show both as not configured
        assert "provider_api_keys" in config
        assert config["provider_api_keys"]["anthropic_configured"] is False
        assert config["provider_api_keys"]["voyageai_configured"] is False

    def test_provider_api_keys_with_none_claude_cli(self):
        """Test that provider_api_keys works when claude_cli is None."""
        # Given: Settings with None claude_cli
        settings = self._create_base_settings(claude_cli_value=None)

        # When: Getting current config
        with patch("code_indexer.server.services.config_service.get_config_service") as mock_service:
            mock_config_service = Mock()
            mock_config_service.get_all_settings.return_value = settings
            mock_service.return_value = mock_config_service

            config = _get_current_config()

        # Then: provider_api_keys should show both as not configured
        assert "provider_api_keys" in config
        assert config["provider_api_keys"]["anthropic_configured"] is False
        assert config["provider_api_keys"]["voyageai_configured"] is False
