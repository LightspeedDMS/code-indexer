"""Story #997 - Unit tests for pace_maker config service integration.

Tests that ConfigService properly handles the pace_maker section:
- pace_maker_mode (runtime setting: "disabled" | "on" | "off")
- pace_maker_clone_path (bootstrap-only setting)
"""

from pathlib import Path

import pytest

from code_indexer.server.services.config_service import BOOTSTRAP_KEYS, ConfigService
from code_indexer.server.utils.config_manager import ServerConfig


class TestPaceMakerConfigDefaults:
    """Test that pace_maker fields have correct defaults."""

    def test_pace_maker_mode_default_disabled(self) -> None:
        """pace_maker_mode must default to 'disabled'."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.pace_maker_mode == "disabled"

    def test_pace_maker_clone_path_default_none(self) -> None:
        """pace_maker_clone_path must default to None."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.pace_maker_clone_path is None


class TestPaceMakerBootstrapKeys:
    """Test bootstrap key classification for pace_maker fields."""

    def test_pace_maker_clone_path_in_bootstrap_keys(self) -> None:
        """pace_maker_clone_path must be in BOOTSTRAP_KEYS (set by auto-updater pre-DB)."""
        assert "pace_maker_clone_path" in BOOTSTRAP_KEYS

    def test_pace_maker_mode_not_in_bootstrap_keys(self) -> None:
        """pace_maker_mode must NOT be in BOOTSTRAP_KEYS (runtime Web UI)."""
        assert "pace_maker_mode" not in BOOTSTRAP_KEYS


class TestPaceMakerConfigUpdate:
    """Test update_setting() for pace_maker category."""

    def _make_service(self, tmp_path: Path) -> ConfigService:
        """Create a ConfigService with a temporary server dir."""
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.config_manager.create_default_config()
        service.config_manager.save_config(config)
        service._config = config
        return service

    def test_update_pace_maker_mode_disabled(self, tmp_path: Path) -> None:
        """Setting pace_maker_mode to 'disabled' must work."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "pace_maker_mode", "disabled")
        assert service._config is not None
        assert service._config.pace_maker_mode == "disabled"

    def test_update_pace_maker_mode_on(self, tmp_path: Path) -> None:
        """Setting pace_maker_mode to 'on' must work."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "pace_maker_mode", "on")
        assert service._config is not None
        assert service._config.pace_maker_mode == "on"

    def test_update_pace_maker_mode_off(self, tmp_path: Path) -> None:
        """Setting pace_maker_mode to 'off' must work."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "pace_maker_mode", "off")
        assert service._config is not None
        assert service._config.pace_maker_mode == "off"

    def test_update_pace_maker_mode_case_insensitive(self, tmp_path: Path) -> None:
        """Setting pace_maker_mode with uppercase must be normalized to lowercase."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "pace_maker_mode", "ON")
        assert service._config is not None
        assert service._config.pace_maker_mode == "on"

    def test_update_pace_maker_mode_invalid_raises(self, tmp_path: Path) -> None:
        """Invalid value for pace_maker_mode must raise ValueError."""
        service = self._make_service(tmp_path)
        with pytest.raises(ValueError, match="Invalid pace_maker_mode"):
            service.update_setting("pace_maker", "pace_maker_mode", "maybe")

    def test_update_pace_maker_unknown_key_raises(self, tmp_path: Path) -> None:
        """Unknown pace_maker key must raise ValueError."""
        service = self._make_service(tmp_path)
        with pytest.raises(ValueError, match="Unknown pace_maker setting"):
            service.update_setting("pace_maker", "nonexistent_key", True)


class TestPaceMakerGetConfig:
    """Test that get_config() returns the pace_maker section."""

    def test_get_config_includes_pace_maker_section(self, tmp_path: Path) -> None:
        """get_all_settings() must return a 'pace_maker' section."""
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.config_manager.create_default_config()
        service.config_manager.save_config(config)
        service._config = config

        settings = service.get_all_settings()
        assert "pace_maker" in settings

    def test_get_config_pace_maker_section_has_mode_field(self, tmp_path: Path) -> None:
        """pace_maker section in get_all_settings() must contain pace_maker_mode."""
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.config_manager.create_default_config()
        service.config_manager.save_config(config)
        service._config = config

        settings = service.get_all_settings()
        assert "pace_maker_mode" in settings["pace_maker"]

    def test_get_config_pace_maker_default_values(self, tmp_path: Path) -> None:
        """pace_maker section in get_all_settings() must reflect ServerConfig defaults."""
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.config_manager.create_default_config()
        service.config_manager.save_config(config)
        service._config = config

        settings = service.get_all_settings()
        assert settings["pace_maker"]["pace_maker_mode"] == "disabled"
