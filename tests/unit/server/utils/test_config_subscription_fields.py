"""
Unit tests for ClaudeIntegrationConfig subscription mode fields (Story #366).

Tests:
- Default values for the 4 new subscription fields
- Save/load roundtrip preserves all new fields correctly
"""

from __future__ import annotations

from code_indexer.server.utils.config_manager import (
    ClaudeIntegrationConfig,
    ServerConfigManager,
)


class TestSubscriptionFieldDefaults:
    """New subscription fields must have correct default values."""

    def test_default_claude_auth_mode_is_api_key(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        assert config.claude_integration_config is not None
        assert config.claude_integration_config.claude_auth_mode == "api_key"

    def test_default_llm_creds_provider_url_is_empty(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        assert config.claude_integration_config.llm_creds_provider_url == ""

    def test_default_llm_creds_provider_api_key_is_empty(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        assert config.claude_integration_config.llm_creds_provider_api_key == ""

    def test_default_llm_creds_provider_consumer_id_is_cidx_server(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        assert config.claude_integration_config.llm_creds_provider_consumer_id == "cidx-server"

    def test_dataclass_default_claude_auth_mode_is_api_key(self):
        cfg = ClaudeIntegrationConfig()
        assert cfg.claude_auth_mode == "api_key"

    def test_dataclass_default_llm_creds_provider_url_is_empty(self):
        cfg = ClaudeIntegrationConfig()
        assert cfg.llm_creds_provider_url == ""

    def test_dataclass_default_llm_creds_provider_api_key_is_empty(self):
        cfg = ClaudeIntegrationConfig()
        assert cfg.llm_creds_provider_api_key == ""

    def test_dataclass_default_consumer_id_is_cidx_server(self):
        cfg = ClaudeIntegrationConfig()
        assert cfg.llm_creds_provider_consumer_id == "cidx-server"


class TestSubscriptionFieldsPersistence:
    """Subscription fields must survive a save/load roundtrip."""

    def test_save_and_load_claude_auth_mode_subscription(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.claude_integration_config.claude_auth_mode = "subscription"
        config_manager.save_config(config)

        loaded = config_manager.load_config()
        assert loaded.claude_integration_config.claude_auth_mode == "subscription"

    def test_save_and_load_llm_creds_provider_url(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.claude_integration_config.llm_creds_provider_url = "http://creds.example.com:8080"
        config_manager.save_config(config)

        loaded = config_manager.load_config()
        assert loaded.claude_integration_config.llm_creds_provider_url == "http://creds.example.com:8080"

    def test_save_and_load_llm_creds_provider_api_key(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.claude_integration_config.llm_creds_provider_api_key = "secret-provider-key"
        config_manager.save_config(config)

        loaded = config_manager.load_config()
        assert loaded.claude_integration_config.llm_creds_provider_api_key == "secret-provider-key"

    def test_save_and_load_llm_creds_provider_consumer_id(self, tmp_path):
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.claude_integration_config.llm_creds_provider_consumer_id = "my-custom-consumer"
        config_manager.save_config(config)

        loaded = config_manager.load_config()
        assert loaded.claude_integration_config.llm_creds_provider_consumer_id == "my-custom-consumer"

    def test_all_subscription_fields_roundtrip(self, tmp_path):
        """All 4 fields survive a save/load roundtrip together."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config.claude_integration_config.claude_auth_mode = "subscription"
        config.claude_integration_config.llm_creds_provider_url = "http://provider:9090"
        config.claude_integration_config.llm_creds_provider_api_key = "my-api-key"
        config.claude_integration_config.llm_creds_provider_consumer_id = "cidx-staging"
        config_manager.save_config(config)

        loaded = config_manager.load_config()
        claude = loaded.claude_integration_config
        assert claude.claude_auth_mode == "subscription"
        assert claude.llm_creds_provider_url == "http://provider:9090"
        assert claude.llm_creds_provider_api_key == "my-api-key"
        assert claude.llm_creds_provider_consumer_id == "cidx-staging"
