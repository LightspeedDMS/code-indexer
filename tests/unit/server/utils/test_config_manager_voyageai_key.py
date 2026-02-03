"""
Unit tests for voyageai_api_key field in ClaudeIntegrationConfig.

Tests cover:
- voyageai_api_key field exists in ClaudeIntegrationConfig
- voyageai_api_key defaults to None
- voyageai_api_key persists through save/load cycle

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import tempfile


from code_indexer.server.utils.config_manager import (
    ClaudeIntegrationConfig,
    ServerConfigManager,
)


class TestClaudeIntegrationConfigVoyageAIKey:
    """Test voyageai_api_key field in ClaudeIntegrationConfig."""

    def test_voyageai_api_key_field_exists(self):
        """AC: ClaudeIntegrationConfig has voyageai_api_key field."""
        config = ClaudeIntegrationConfig()
        assert hasattr(config, "voyageai_api_key")

    def test_voyageai_api_key_defaults_to_none(self):
        """AC: voyageai_api_key defaults to None."""
        config = ClaudeIntegrationConfig()
        assert config.voyageai_api_key is None

    def test_voyageai_api_key_can_be_set(self):
        """voyageai_api_key can be set to a value."""
        config = ClaudeIntegrationConfig(voyageai_api_key="pa-testvoyagekey123")
        assert config.voyageai_api_key == "pa-testvoyagekey123"


class TestServerConfigVoyageAIKeyPersistence:
    """Test voyageai_api_key persistence through ServerConfigManager."""

    def test_voyageai_api_key_persists_through_save_load(self):
        """AC: voyageai_api_key persists through save/load cycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            # Create config with voyageai_api_key
            config = manager.create_default_config()
            config.claude_integration_config.voyageai_api_key = "pa-persistedkey12345"

            # Save and reload
            manager.save_config(config)
            loaded_config = manager.load_config()

            assert loaded_config is not None
            assert (
                loaded_config.claude_integration_config.voyageai_api_key
                == "pa-persistedkey12345"
            )

    def test_voyageai_api_key_in_settings_dict(self):
        """voyageai_api_key is included in get_all_settings claude_cli section."""
        from code_indexer.server.services.config_service import ConfigService

        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(server_dir_path=tmpdir)
            config = service.load_config()
            config.claude_integration_config.voyageai_api_key = "pa-settingskey12345"
            service.config_manager.save_config(config)

            settings = service.get_all_settings()

            assert "claude_cli" in settings
            assert "voyageai_api_key" in settings["claude_cli"]
