"""Story #997 - Unit tests for pace_maker config service integration.

Tests that ConfigService properly handles the pace_maker section:
- enforce_pace_maker_pacing_only (runtime setting)
- pace_maker_clone_path (bootstrap-only setting)
"""

from pathlib import Path

import pytest

from code_indexer.server.services.config_service import BOOTSTRAP_KEYS, ConfigService
from code_indexer.server.utils.config_manager import ServerConfig


class TestPaceMakerConfigDefaults:
    """Test that pace_maker fields have correct defaults."""

    def test_enforce_pace_maker_default_false(self) -> None:
        """enforce_pace_maker_pacing_only must default to False."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.enforce_pace_maker_pacing_only is False

    def test_pace_maker_clone_path_default_none(self) -> None:
        """pace_maker_clone_path must default to None."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.pace_maker_clone_path is None


class TestPaceMakerBootstrapKeys:
    """Test bootstrap key classification for pace_maker fields."""

    def test_pace_maker_clone_path_in_bootstrap_keys(self) -> None:
        """pace_maker_clone_path must be in BOOTSTRAP_KEYS (set by auto-updater pre-DB)."""
        assert "pace_maker_clone_path" in BOOTSTRAP_KEYS

    def test_enforce_pace_maker_not_in_bootstrap_keys(self) -> None:
        """enforce_pace_maker_pacing_only must NOT be in BOOTSTRAP_KEYS (runtime Web UI)."""
        assert "enforce_pace_maker_pacing_only" not in BOOTSTRAP_KEYS


class TestPaceMakerConfigUpdate:
    """Test update_setting() for pace_maker category."""

    def _make_service(self, tmp_path: Path) -> ConfigService:
        """Create a ConfigService with a temporary server dir."""
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.config_manager.create_default_config()
        service.config_manager.save_config(config)
        service._config = config
        return service

    def test_update_pace_maker_setting_true_bool(self, tmp_path: Path) -> None:
        """Setting enforce_pace_maker_pacing_only to bool True must work."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "enforce_pace_maker_pacing_only", True)
        assert service._config is not None
        assert service._config.enforce_pace_maker_pacing_only is True

    def test_update_pace_maker_setting_false_bool(self, tmp_path: Path) -> None:
        """Setting enforce_pace_maker_pacing_only to bool False must work."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "enforce_pace_maker_pacing_only", True)
        service.update_setting("pace_maker", "enforce_pace_maker_pacing_only", False)
        assert service._config is not None
        assert service._config.enforce_pace_maker_pacing_only is False

    def test_update_pace_maker_setting_true_string(self, tmp_path: Path) -> None:
        """Setting enforce_pace_maker_pacing_only to 'true' string must work."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "enforce_pace_maker_pacing_only", "true")
        assert service._config is not None
        assert service._config.enforce_pace_maker_pacing_only is True

    def test_update_pace_maker_setting_false_string(self, tmp_path: Path) -> None:
        """Setting enforce_pace_maker_pacing_only to 'off' string must work."""
        service = self._make_service(tmp_path)
        service.update_setting("pace_maker", "enforce_pace_maker_pacing_only", True)
        service.update_setting("pace_maker", "enforce_pace_maker_pacing_only", "off")
        assert service._config is not None
        assert service._config.enforce_pace_maker_pacing_only is False

    def test_update_pace_maker_setting_invalid_raises(self, tmp_path: Path) -> None:
        """Invalid value for enforce_pace_maker_pacing_only must raise ValueError."""
        service = self._make_service(tmp_path)
        with pytest.raises(
            ValueError, match="Invalid value for enforce_pace_maker_pacing_only"
        ):
            service.update_setting(
                "pace_maker", "enforce_pace_maker_pacing_only", "maybe"
            )

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

    def test_get_config_pace_maker_section_has_enforce_field(
        self, tmp_path: Path
    ) -> None:
        """pace_maker section in get_all_settings() must contain enforce_pace_maker_pacing_only."""
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.config_manager.create_default_config()
        service.config_manager.save_config(config)
        service._config = config

        settings = service.get_all_settings()
        assert "enforce_pace_maker_pacing_only" in settings["pace_maker"]

    def test_get_config_pace_maker_default_values(self, tmp_path: Path) -> None:
        """pace_maker section in get_all_settings() must reflect ServerConfig defaults."""
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.config_manager.create_default_config()
        service.config_manager.save_config(config)
        service._config = config

        settings = service.get_all_settings()
        assert settings["pace_maker"]["enforce_pace_maker_pacing_only"] is False
