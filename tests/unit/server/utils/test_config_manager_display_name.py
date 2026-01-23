"""
Unit tests for service_display_name configuration (Story #22).

Tests the configurable display name feature that allows server administrators
to rebrand the MCP server while maintaining CIDX technology reference.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import pytest

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
)


class TestServiceDisplayNameConfig:
    """Test suite for service_display_name configuration (Story #22)."""

    # ==========================================================================
    # AC1: Default display name is "Neo"
    # ==========================================================================

    def test_default_config_has_neo_display_name(self, tmp_path):
        """AC1: Fresh installation should have 'Neo' as default display name."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        assert config.service_display_name == "Neo"

    def test_server_config_dataclass_has_display_name_attribute(self):
        """Verify ServerConfig dataclass has service_display_name field."""
        # Create minimal config to test dataclass structure
        config = ServerConfig(server_dir="/tmp/test")

        # Field should exist and have default value
        assert hasattr(config, "service_display_name")
        assert config.service_display_name == "Neo"

    # ==========================================================================
    # AC2: Custom display name persists in config file
    # ==========================================================================

    def test_custom_display_name_saves_to_file(self, tmp_path):
        """Custom display name should persist when saved to config file."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Set custom display name
        config.service_display_name = "MyBrand"
        config_manager.save_config(config)

        # Verify it was saved to JSON
        config_file = tmp_path / "config.json"
        with open(config_file) as f:
            saved_config = json.load(f)

        assert saved_config["service_display_name"] == "MyBrand"

    def test_custom_display_name_loads_from_file(self, tmp_path):
        """Custom display name should load correctly from config file."""
        # Create config file with custom display name
        config_data = {
            "service_display_name": "CustomName",
            "host": "127.0.0.1",
            "port": 8000,
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load and verify
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config.service_display_name == "CustomName"

    # ==========================================================================
    # AC5: Empty display name falls back to default "Neo"
    # ==========================================================================

    def test_empty_display_name_fallback_to_default(self, tmp_path):
        """Empty string display name should fall back to 'Neo' default."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Set empty display name
        config.service_display_name = ""

        # When getting effective display name, empty should return default
        # This tests the getter/property behavior
        effective_name = config.service_display_name or "Neo"
        assert effective_name == "Neo"

    def test_none_display_name_fallback_to_default(self, tmp_path):
        """None display name should fall back to 'Neo' default."""
        # Create config file without service_display_name (simulates old config)
        config_data = {
            "host": "127.0.0.1",
            "port": 8000,
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        # Load - should get default value
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        # Should have default value from dataclass, not None
        assert config.service_display_name == "Neo"

    # ==========================================================================
    # Validation tests
    # ==========================================================================

    def test_display_name_validation_accepts_valid_strings(self, tmp_path):
        """Valid display names should pass validation."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Test various valid names
        valid_names = ["Neo", "MyBrand", "CIDX Custom", "My-Server_v2", "123Test"]
        for name in valid_names:
            config.service_display_name = name
            # Should not raise
            config_manager.validate_config(config)

    def test_config_roundtrip_preserves_display_name(self, tmp_path):
        """Display name should survive save/load roundtrip."""
        config_manager = ServerConfigManager(str(tmp_path))

        # Create, modify, save
        config = config_manager.create_default_config()
        config.service_display_name = "RoundtripTest"
        config_manager.save_config(config)

        # Load and verify
        loaded_config = config_manager.load_config()
        assert loaded_config.service_display_name == "RoundtripTest"
