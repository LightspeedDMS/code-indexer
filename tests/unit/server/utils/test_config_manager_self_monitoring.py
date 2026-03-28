"""
Unit tests for SelfMonitoringConfig dataclass (Story #72 - AC1).

Tests self-monitoring configuration including:
- Default values for all fields
- Field types and validation
- Integration with ServerConfig __post_init__
"""

import json
from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    SelfMonitoringConfig,
)


class TestSelfMonitoringConfig:
    """Test suite for SelfMonitoringConfig dataclass."""

    def test_self_monitoring_config_defaults(self):
        """Test SelfMonitoringConfig has correct default values.

        Story #566: prompt_template and prompt_user_modified fields removed.
        """
        config = SelfMonitoringConfig()

        assert config.enabled is False
        assert config.cadence_minutes == 60
        assert config.model == "opus"
        assert not hasattr(config, "prompt_template")
        assert not hasattr(config, "prompt_user_modified")

    def test_self_monitoring_config_custom_values(self):
        """Test SelfMonitoringConfig accepts custom values.

        Story #566: prompt_template and prompt_user_modified removed; only
        enabled, cadence_minutes, and model are user-configurable.
        """
        config = SelfMonitoringConfig(
            enabled=True,
            cadence_minutes=30,
            model="sonnet",
        )

        assert config.enabled is True
        assert config.cadence_minutes == 30
        assert config.model == "sonnet"

    def test_server_config_initializes_self_monitoring_config(self, tmp_path):
        """Test ServerConfig.__post_init__ initializes self_monitoring_config if None."""
        config = ServerConfig(server_dir=str(tmp_path))

        assert config.self_monitoring_config is not None
        assert isinstance(config.self_monitoring_config, SelfMonitoringConfig)
        assert config.self_monitoring_config.enabled is False
        assert config.self_monitoring_config.cadence_minutes == 60

    def test_server_config_preserves_existing_self_monitoring_config(self, tmp_path):
        """Test ServerConfig.__post_init__ preserves existing self_monitoring_config."""
        custom_config = SelfMonitoringConfig(
            enabled=True, cadence_minutes=45, model="haiku"
        )
        config = ServerConfig(
            server_dir=str(tmp_path), self_monitoring_config=custom_config
        )

        assert config.self_monitoring_config is custom_config
        assert config.self_monitoring_config.enabled is True
        assert config.self_monitoring_config.cadence_minutes == 45
        assert config.self_monitoring_config.model == "haiku"

    def test_self_monitoring_config_serialization(self, tmp_path):
        """Test SelfMonitoringConfig can be saved and loaded from JSON.

        Story #566: prompt_template and prompt_user_modified no longer persisted.
        Only enabled, cadence_minutes, and model are serialized.
        """
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Customize self_monitoring_config
        assert config.self_monitoring_config is not None
        config.self_monitoring_config.enabled = True
        config.self_monitoring_config.cadence_minutes = 90

        # Save and reload
        config_manager.save_config(config)
        loaded_config = config_manager.load_config()

        assert loaded_config is not None
        assert loaded_config.self_monitoring_config is not None
        assert loaded_config.self_monitoring_config.enabled is True
        assert loaded_config.self_monitoring_config.cadence_minutes == 90
        assert not hasattr(loaded_config.self_monitoring_config, "prompt_template")
        assert not hasattr(loaded_config.self_monitoring_config, "prompt_user_modified")

    def test_self_monitoring_config_dict_conversion(self, tmp_path):
        """Test SelfMonitoringConfig dict conversion in load_config.

        Story #566: prompt_template and prompt_user_modified are stripped when
        loading from JSON to ensure backward compatibility with old config files.
        """
        # Create config file with self_monitoring_config as dict
        # (includes stale prompt fields that should be ignored)
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "self_monitoring_config": {
                "enabled": True,
                "cadence_minutes": 120,
                "model": "sonnet",
                "prompt_template": "Test",  # stale field — must be ignored
                "prompt_user_modified": True,  # stale field — must be ignored
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config is not None
        assert config.self_monitoring_config is not None
        assert isinstance(config.self_monitoring_config, SelfMonitoringConfig)
        assert config.self_monitoring_config.enabled is True
        assert config.self_monitoring_config.cadence_minutes == 120
        assert config.self_monitoring_config.model == "sonnet"
        # Stale fields must not appear on the resulting object
        assert not hasattr(config.self_monitoring_config, "prompt_template")
        assert not hasattr(config.self_monitoring_config, "prompt_user_modified")

    def test_all_self_monitoring_fields_roundtrip(self, tmp_path):
        """Test that ALL SelfMonitoringConfig fields persist through save/load cycle.

        Story #566: prompt_template and prompt_user_modified removed.
        Verifies the three remaining user-configurable fields (enabled, cadence_minutes,
        model) roundtrip correctly, and that the removed fields are absent.
        """
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Customize all remaining fields in self_monitoring_config
        assert config.self_monitoring_config is not None
        config.self_monitoring_config.enabled = True
        config.self_monitoring_config.cadence_minutes = 45
        config.self_monitoring_config.model = "sonnet"

        # Save and reload
        config_manager.save_config(config)
        loaded_config = config_manager.load_config()

        # Verify all remaining fields persisted correctly
        assert loaded_config is not None
        assert loaded_config.self_monitoring_config is not None
        assert loaded_config.self_monitoring_config.enabled is True
        assert loaded_config.self_monitoring_config.cadence_minutes == 45
        assert loaded_config.self_monitoring_config.model == "sonnet"
        # Removed fields must not exist
        assert not hasattr(loaded_config.self_monitoring_config, "prompt_template")
        assert not hasattr(loaded_config.self_monitoring_config, "prompt_user_modified")
