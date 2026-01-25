"""
Tests for ServerConfig Claude CLI fields (Story #546 - AC1, updated for Story #15).

Story #15 moved Claude CLI fields from ServerConfig to ClaudeIntegrationConfig.
These tests verify the NEW architecture where fields are accessed via:
  config.claude_integration_config.anthropic_api_key
  config.claude_integration_config.max_concurrent_claude_cli
  config.claude_integration_config.description_refresh_interval_hours

Story #24 changed default max_concurrent_claude_cli from 4 to 2.

All tests use real components following MESSI Rule #1: No mocks.
"""

from src.code_indexer.server.utils.config_manager import ServerConfig, ClaudeIntegrationConfig


# =============================================================================
# AC1: ClaudeIntegrationConfig has fields with correct defaults (Story #15)
# =============================================================================


class TestClaudeIntegrationConfigFields:
    """Tests for Claude CLI fields in ClaudeIntegrationConfig (Story #15)."""

    def test_claude_integration_config_has_anthropic_api_key_field(self):
        """
        AC1: ClaudeIntegrationConfig has anthropic_api_key field with None default.

        Given I create a new ClaudeIntegrationConfig
        When I inspect the anthropic_api_key field
        Then it exists and defaults to None
        """
        config = ClaudeIntegrationConfig()

        assert hasattr(
            config, "anthropic_api_key"
        ), "ClaudeIntegrationConfig should have anthropic_api_key field"
        assert (
            config.anthropic_api_key is None
        ), "anthropic_api_key should default to None"

    def test_claude_integration_config_has_max_concurrent_claude_cli_field(self):
        """
        AC1: ClaudeIntegrationConfig has max_concurrent_claude_cli field with default 2.

        Given I create a new ClaudeIntegrationConfig
        When I inspect the max_concurrent_claude_cli field
        Then it exists and defaults to 2

        Note: Story #24 changed default from 4 to 2 for resource-constrained systems.
        """
        config = ClaudeIntegrationConfig()

        assert hasattr(
            config, "max_concurrent_claude_cli"
        ), "ClaudeIntegrationConfig should have max_concurrent_claude_cli field"
        # Story #24: Default changed from 4 to 2 for resource-constrained systems
        assert (
            config.max_concurrent_claude_cli == 2
        ), "max_concurrent_claude_cli should default to 2"

    def test_claude_integration_config_has_description_refresh_interval_hours_field(self):
        """
        AC1: ClaudeIntegrationConfig has description_refresh_interval_hours field with default 24.

        Given I create a new ClaudeIntegrationConfig
        When I inspect the description_refresh_interval_hours field
        Then it exists and defaults to 24
        """
        config = ClaudeIntegrationConfig()

        assert hasattr(
            config, "description_refresh_interval_hours"
        ), "ClaudeIntegrationConfig should have description_refresh_interval_hours field"
        assert (
            config.description_refresh_interval_hours == 24
        ), "description_refresh_interval_hours should default to 24"

    def test_claude_integration_config_accepts_custom_anthropic_api_key(self):
        """
        AC1: ClaudeIntegrationConfig accepts custom anthropic_api_key value.

        Given I create a ClaudeIntegrationConfig with custom anthropic_api_key
        When I inspect the field
        Then it has the custom value
        """
        config = ClaudeIntegrationConfig(anthropic_api_key="sk-ant-test-key-123")

        assert (
            config.anthropic_api_key == "sk-ant-test-key-123"
        ), "anthropic_api_key should accept custom value"

    def test_claude_integration_config_accepts_custom_max_concurrent_claude_cli(self):
        """
        AC1: ClaudeIntegrationConfig accepts custom max_concurrent_claude_cli value.

        Given I create a ClaudeIntegrationConfig with custom max_concurrent_claude_cli
        When I inspect the field
        Then it has the custom value
        """
        config = ClaudeIntegrationConfig(max_concurrent_claude_cli=8)

        assert (
            config.max_concurrent_claude_cli == 8
        ), "max_concurrent_claude_cli should accept custom value"

    def test_claude_integration_config_accepts_custom_description_refresh_interval_hours(self):
        """
        AC1: ClaudeIntegrationConfig accepts custom description_refresh_interval_hours value.

        Given I create a ClaudeIntegrationConfig with custom description_refresh_interval_hours
        When I inspect the field
        Then it has the custom value
        """
        config = ClaudeIntegrationConfig(description_refresh_interval_hours=48)

        assert (
            config.description_refresh_interval_hours == 48
        ), "description_refresh_interval_hours should accept custom value"


class TestServerConfigClaudeIntegrationAccess:
    """Tests for accessing Claude CLI fields via ServerConfig.claude_integration_config."""

    def test_serverconfig_has_claude_integration_config(self):
        """
        Story #15: ServerConfig has claude_integration_config field.

        Given I create a ServerConfig
        When I inspect claude_integration_config
        Then it exists and is a ClaudeIntegrationConfig
        """
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(
            config, "claude_integration_config"
        ), "ServerConfig should have claude_integration_config field"
        assert config.claude_integration_config is not None
        assert isinstance(config.claude_integration_config, ClaudeIntegrationConfig)

    def test_serverconfig_claude_integration_has_defaults(self):
        """
        Story #15: ServerConfig.claude_integration_config has correct defaults.

        Given I create a ServerConfig
        When I inspect claude_integration_config fields
        Then they have the correct default values

        Note: Story #24 changed max_concurrent_claude_cli default from 4 to 2.
        """
        config = ServerConfig(server_dir="/tmp/test")
        claude_config = config.claude_integration_config

        assert claude_config.anthropic_api_key is None
        # Story #24: Default changed from 4 to 2
        assert claude_config.max_concurrent_claude_cli == 2
        assert claude_config.description_refresh_interval_hours == 24
