"""
Tests for ServerConfig persistence and validation (Story #546 - AC2).

These tests follow TDD methodology - tests are written FIRST before implementation.
All tests use real components following MESSI Rule #1: No mocks.

Note: Story #15 moved Claude CLI settings to ClaudeIntegrationConfig.
"""

import json
import tempfile

import pytest

from src.code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerConfigManager,
    ClaudeIntegrationConfig,
)


# =============================================================================
# AC2: ServerConfig serialization includes new fields
# =============================================================================


class TestServerConfigSerialization:
    """Tests for serialization of new Claude CLI fields."""

    def test_serverconfig_serialization_includes_anthropic_api_key(self):
        """
        AC2: Serialization includes anthropic_api_key field.

        Given I create a ServerConfig with anthropic_api_key set
        When I serialize to JSON via ServerConfigManager
        Then the JSON includes the anthropic_api_key field

        Note: Story #15 moved anthropic_api_key to ClaudeIntegrationConfig.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            # Story #15: anthropic_api_key moved to claude_integration_config
            config = ServerConfig(
                server_dir=tmpdir,
                claude_integration_config=ClaudeIntegrationConfig(
                    anthropic_api_key="sk-ant-test-key-123"
                ),
            )

            manager.save_config(config)

            # Read raw JSON to verify serialization
            with open(manager.config_file_path, "r") as f:
                config_dict = json.load(f)

            # Story #15: anthropic_api_key is nested in claude_integration_config
            assert (
                "claude_integration_config" in config_dict
            ), "Serialized config should include claude_integration_config"
            assert (
                "anthropic_api_key" in config_dict["claude_integration_config"]
            ), "Serialized config should include anthropic_api_key in claude_integration_config"
            assert (
                config_dict["claude_integration_config"]["anthropic_api_key"]
                == "sk-ant-test-key-123"
            ), "Serialized anthropic_api_key should match original value"

    def test_serverconfig_serialization_includes_max_concurrent_claude_cli(self):
        """
        AC2: Serialization includes max_concurrent_claude_cli field.

        Given I create a ServerConfig with max_concurrent_claude_cli set
        When I serialize to JSON via ServerConfigManager
        Then the JSON includes the max_concurrent_claude_cli field

        Note: Story #15 moved max_concurrent_claude_cli to ClaudeIntegrationConfig.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            # Story #15: max_concurrent_claude_cli moved to claude_integration_config
            config = ServerConfig(
                server_dir=tmpdir,
                claude_integration_config=ClaudeIntegrationConfig(
                    max_concurrent_claude_cli=8
                ),
            )

            manager.save_config(config)

            # Read raw JSON to verify serialization
            with open(manager.config_file_path, "r") as f:
                config_dict = json.load(f)

            # Story #15: max_concurrent_claude_cli is nested in claude_integration_config
            assert (
                "claude_integration_config" in config_dict
            ), "Serialized config should include claude_integration_config"
            assert (
                "max_concurrent_claude_cli" in config_dict["claude_integration_config"]
            ), "Serialized config should include max_concurrent_claude_cli in claude_integration_config"
            assert (
                config_dict["claude_integration_config"]["max_concurrent_claude_cli"]
                == 8
            ), "Serialized max_concurrent_claude_cli should match original value"

    def test_serverconfig_serialization_includes_description_refresh_interval_hours(
        self,
    ):
        """
        AC2: Serialization includes description_refresh_interval_hours field.

        Given I create a ServerConfig with description_refresh_interval_hours set
        When I serialize to JSON via ServerConfigManager
        Then the JSON includes the description_refresh_interval_hours field

        Note: Story #15 moved description_refresh_interval_hours to ClaudeIntegrationConfig.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            # Story #15: description_refresh_interval_hours moved to claude_integration_config
            config = ServerConfig(
                server_dir=tmpdir,
                claude_integration_config=ClaudeIntegrationConfig(
                    description_refresh_interval_hours=48
                ),
            )

            manager.save_config(config)

            # Read raw JSON to verify serialization
            with open(manager.config_file_path, "r") as f:
                config_dict = json.load(f)

            # Story #15: description_refresh_interval_hours is nested in claude_integration_config
            assert (
                "claude_integration_config" in config_dict
            ), "Serialized config should include claude_integration_config"
            assert (
                "description_refresh_interval_hours"
                in config_dict["claude_integration_config"]
            ), "Serialized config should include description_refresh_interval_hours in claude_integration_config"
            assert (
                config_dict["claude_integration_config"][
                    "description_refresh_interval_hours"
                ]
                == 48
            ), "Serialized description_refresh_interval_hours should match original value"


# =============================================================================
# AC2: ServerConfig deserialization handles missing fields (backward compat)
# =============================================================================


class TestServerConfigBackwardCompatibility:
    """Tests for backward compatibility with old config files."""

    def test_deserialize_old_config_without_anthropic_api_key(self):
        """
        AC2: Old config files without anthropic_api_key load with None default.

        Given I have an old config.json without anthropic_api_key
        When I load it via ServerConfigManager
        Then it loads successfully with anthropic_api_key = None

        Note: Story #15 moved anthropic_api_key to ClaudeIntegrationConfig.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            # Create old config file WITHOUT new fields
            old_config = {
                "server_dir": tmpdir,
                "host": "127.0.0.1",
                "port": 8000,
                "workers": 4,
            }

            with open(manager.config_file_path, "w") as f:
                json.dump(old_config, f)

            # Load config
            config = manager.load_config()

            assert config is not None, "Old config should load successfully"
            # Story #15: anthropic_api_key moved to claude_integration_config
            assert (
                config.claude_integration_config.anthropic_api_key is None
            ), "Missing anthropic_api_key should default to None"

    def test_deserialize_old_config_without_max_concurrent_claude_cli(self):
        """
        AC2: Old config files without max_concurrent_claude_cli load with default 4.

        Given I have an old config.json without max_concurrent_claude_cli
        When I load it via ServerConfigManager
        Then it loads successfully with max_concurrent_claude_cli = 4

        Note: Story #15 moved max_concurrent_claude_cli to ClaudeIntegrationConfig.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            # Create old config file WITHOUT new fields
            old_config = {
                "server_dir": tmpdir,
                "host": "127.0.0.1",
                "port": 8000,
                "workers": 4,
            }

            with open(manager.config_file_path, "w") as f:
                json.dump(old_config, f)

            # Load config
            config = manager.load_config()

            assert config is not None, "Old config should load successfully"
            # Story #15: max_concurrent_claude_cli moved to claude_integration_config
            assert (
                config.claude_integration_config.max_concurrent_claude_cli == 4
            ), "Missing max_concurrent_claude_cli should default to 4"

    def test_deserialize_old_config_without_description_refresh_interval_hours(self):
        """
        AC2: Old config files without description_refresh_interval_hours load with default 24.

        Given I have an old config.json without description_refresh_interval_hours
        When I load it via ServerConfigManager
        Then it loads successfully with description_refresh_interval_hours = 24

        Note: Story #15 moved description_refresh_interval_hours to ClaudeIntegrationConfig.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            # Create old config file WITHOUT new fields
            old_config = {
                "server_dir": tmpdir,
                "host": "127.0.0.1",
                "port": 8000,
                "workers": 4,
            }

            with open(manager.config_file_path, "w") as f:
                json.dump(old_config, f)

            # Load config
            config = manager.load_config()

            assert config is not None, "Old config should load successfully"
            # Story #15: description_refresh_interval_hours moved to claude_integration_config
            assert (
                config.claude_integration_config.description_refresh_interval_hours
                == 24
            ), "Missing description_refresh_interval_hours should default to 24"

    def test_roundtrip_serialization_preserves_new_fields(self):
        """
        AC2: Save + Load roundtrip preserves all new fields.

        Given I create a ServerConfig with all new fields set
        When I save and reload it
        Then all new fields are preserved

        Note: Story #15 moved Claude CLI settings to ClaudeIntegrationConfig.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)

            # Story #15: Claude CLI settings moved to claude_integration_config
            original_config = ServerConfig(
                server_dir=tmpdir,
                claude_integration_config=ClaudeIntegrationConfig(
                    anthropic_api_key="sk-ant-test-key-123",
                    max_concurrent_claude_cli=8,
                    description_refresh_interval_hours=48,
                ),
            )

            # Save
            manager.save_config(original_config)

            # Load
            loaded_config = manager.load_config()

            assert loaded_config is not None, "Config should reload successfully"
            # Story #15: access via claude_integration_config
            assert (
                loaded_config.claude_integration_config.anthropic_api_key
                == "sk-ant-test-key-123"
            ), "anthropic_api_key should be preserved"
            assert (
                loaded_config.claude_integration_config.max_concurrent_claude_cli == 8
            ), "max_concurrent_claude_cli should be preserved"
            assert (
                loaded_config.claude_integration_config.description_refresh_interval_hours
                == 48
            ), "description_refresh_interval_hours should be preserved"


# =============================================================================
# Validation Tests
# =============================================================================


class TestServerConfigValidation:
    """Tests for validation of new Claude CLI fields."""

    def test_validation_rejects_max_concurrent_claude_cli_less_than_1(self):
        """
        Validation rejects max_concurrent_claude_cli < 1.

        Given I create a ServerConfig with max_concurrent_claude_cli = 0
        When I validate the config
        Then it raises ValueError

        Note: Story #15 moved max_concurrent_claude_cli to ClaudeIntegrationConfig.
        """
        # Story #15: max_concurrent_claude_cli moved to claude_integration_config
        config = ServerConfig(
            server_dir="/tmp/test",
            claude_integration_config=ClaudeIntegrationConfig(
                max_concurrent_claude_cli=0
            ),
        )

        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError) as exc_info:
            manager.validate_config(config)

        assert (
            "max_concurrent_claude_cli" in str(exc_info.value).lower()
        ), "Error message should mention max_concurrent_claude_cli"

    def test_validation_rejects_max_concurrent_claude_cli_negative(self):
        """
        Validation rejects negative max_concurrent_claude_cli.

        Given I create a ServerConfig with max_concurrent_claude_cli = -1
        When I validate the config
        Then it raises ValueError

        Note: Story #15 moved max_concurrent_claude_cli to ClaudeIntegrationConfig.
        """
        # Story #15: max_concurrent_claude_cli moved to claude_integration_config
        config = ServerConfig(
            server_dir="/tmp/test",
            claude_integration_config=ClaudeIntegrationConfig(
                max_concurrent_claude_cli=-1
            ),
        )

        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError) as exc_info:
            manager.validate_config(config)

        assert (
            "max_concurrent_claude_cli" in str(exc_info.value).lower()
        ), "Error message should mention max_concurrent_claude_cli"

    def test_validation_rejects_description_refresh_interval_hours_less_than_1(self):
        """
        Validation rejects description_refresh_interval_hours < 1.

        Given I create a ServerConfig with description_refresh_interval_hours = 0
        When I validate the config
        Then it raises ValueError

        Note: Story #15 moved description_refresh_interval_hours to ClaudeIntegrationConfig.
        """
        # Story #15: description_refresh_interval_hours moved to claude_integration_config
        config = ServerConfig(
            server_dir="/tmp/test",
            claude_integration_config=ClaudeIntegrationConfig(
                description_refresh_interval_hours=0
            ),
        )

        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError) as exc_info:
            manager.validate_config(config)

        assert (
            "description_refresh_interval_hours" in str(exc_info.value).lower()
        ), "Error message should mention description_refresh_interval_hours"

    def test_validation_rejects_description_refresh_interval_hours_negative(self):
        """
        Validation rejects negative description_refresh_interval_hours.

        Given I create a ServerConfig with description_refresh_interval_hours = -1
        When I validate the config
        Then it raises ValueError

        Note: Story #15 moved description_refresh_interval_hours to ClaudeIntegrationConfig.
        """
        # Story #15: description_refresh_interval_hours moved to claude_integration_config
        config = ServerConfig(
            server_dir="/tmp/test",
            claude_integration_config=ClaudeIntegrationConfig(
                description_refresh_interval_hours=-1
            ),
        )

        manager = ServerConfigManager("/tmp/test")

        with pytest.raises(ValueError) as exc_info:
            manager.validate_config(config)

        assert (
            "description_refresh_interval_hours" in str(exc_info.value).lower()
        ), "Error message should mention description_refresh_interval_hours"

    def test_validation_accepts_valid_max_concurrent_claude_cli(self):
        """
        Validation accepts valid max_concurrent_claude_cli values.

        Given I create a ServerConfig with max_concurrent_claude_cli = 4
        When I validate the config
        Then it passes without error

        Note: Story #15 moved max_concurrent_claude_cli to ClaudeIntegrationConfig.
        """
        # Story #15: max_concurrent_claude_cli moved to claude_integration_config
        config = ServerConfig(
            server_dir="/tmp/test",
            claude_integration_config=ClaudeIntegrationConfig(
                max_concurrent_claude_cli=4
            ),
        )

        manager = ServerConfigManager("/tmp/test")

        # Should not raise
        manager.validate_config(config)

    def test_validation_accepts_valid_description_refresh_interval_hours(self):
        """
        Validation accepts valid description_refresh_interval_hours values.

        Given I create a ServerConfig with description_refresh_interval_hours = 24
        When I validate the config
        Then it passes without error

        Note: Story #15 moved description_refresh_interval_hours to ClaudeIntegrationConfig.
        """
        # Story #15: description_refresh_interval_hours moved to claude_integration_config
        config = ServerConfig(
            server_dir="/tmp/test",
            claude_integration_config=ClaudeIntegrationConfig(
                description_refresh_interval_hours=24
            ),
        )

        manager = ServerConfigManager("/tmp/test")

        # Should not raise
        manager.validate_config(config)
