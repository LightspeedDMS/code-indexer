"""
Unit tests for Story #967: ActivatedReaperConfig dataclass and ConfigService persistence.

TDD: Tests written BEFORE implementation. All should fail (red phase) until
ActivatedReaperConfig is added to config_manager.py and ConfigService is updated.

Acceptance Criteria covered:
  AC4 - TTL and cadence configurable via Web UI (ConfigService persistence)
"""

import pytest


# ---------------------------------------------------------------------------
# ActivatedReaperConfig dataclass
# ---------------------------------------------------------------------------


class TestActivatedReaperConfigDefaults:
    """ActivatedReaperConfig must have correct default values."""

    def test_defaults_are_correct(self):
        """ActivatedReaperConfig defaults: ttl_days=30, cadence_hours=24."""
        from code_indexer.server.utils.config_manager import ActivatedReaperConfig

        cfg = ActivatedReaperConfig()
        assert cfg.ttl_days == 30
        assert cfg.cadence_hours == 24

    def test_can_override_defaults(self):
        """ActivatedReaperConfig accepts custom values."""
        from code_indexer.server.utils.config_manager import ActivatedReaperConfig

        cfg = ActivatedReaperConfig(ttl_days=60, cadence_hours=12)
        assert cfg.ttl_days == 60
        assert cfg.cadence_hours == 12


# ---------------------------------------------------------------------------
# ServerConfig field presence
# ---------------------------------------------------------------------------


class TestServerConfigActivatedReaperField:
    """ServerConfig must declare activated_reaper_config field."""

    def test_server_config_has_activated_reaper_config_field(self):
        """ServerConfig dataclass must have an activated_reaper_config field."""
        from code_indexer.server.utils.config_manager import ServerConfig

        assert hasattr(ServerConfig, "__dataclass_fields__")
        assert "activated_reaper_config" in ServerConfig.__dataclass_fields__

    def test_post_init_initializes_activated_reaper_config(self, tmp_path):
        """ServerConfig.__post_init__ sets activated_reaper_config to default when None."""
        from code_indexer.server.utils.config_manager import (
            ActivatedReaperConfig,
            ServerConfigManager,
        )

        manager = ServerConfigManager(str(tmp_path))
        config = manager.create_default_config()

        assert config.activated_reaper_config is not None
        assert isinstance(config.activated_reaper_config, ActivatedReaperConfig)
        assert config.activated_reaper_config.ttl_days == 30
        assert config.activated_reaper_config.cadence_hours == 24

    def test_dict_to_server_config_deserializes_activated_reaper(self, tmp_path):
        """_dict_to_server_config converts activated_reaper_config dict to dataclass."""
        from code_indexer.server.utils.config_manager import (
            ActivatedReaperConfig,
            ServerConfigManager,
        )

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "activated_reaper_config": {"ttl_days": 45, "cadence_hours": 6},
        }
        config = manager._dict_to_server_config(config_dict)

        assert isinstance(config.activated_reaper_config, ActivatedReaperConfig)
        assert config.activated_reaper_config.ttl_days == 45
        assert config.activated_reaper_config.cadence_hours == 6


# ---------------------------------------------------------------------------
# ConfigService persistence
# ---------------------------------------------------------------------------


class TestActivatedReaperConfigServicePersistence:
    """ConfigService.get_all_settings() and update_setting() for activated_reaper."""

    def test_get_settings_includes_activated_reaper_section(self, tmp_path):
        """ConfigService.get_all_settings() includes activated_reaper section."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        settings = config_service.get_all_settings()

        assert "activated_reaper" in settings

    def test_get_settings_activated_reaper_has_required_keys(self, tmp_path):
        """activated_reaper section must have ttl_days and cadence_hours keys."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        ar = config_service.get_all_settings()["activated_reaper"]

        assert "ttl_days" in ar
        assert "cadence_hours" in ar

    def test_get_settings_activated_reaper_default_values(self, tmp_path):
        """activated_reaper section returns correct default values."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        ar = config_service.get_all_settings()["activated_reaper"]

        assert ar["ttl_days"] == 30
        assert ar["cadence_hours"] == 24

    def test_update_ttl_days_persists(self, tmp_path):
        """update_setting('activated_reaper', 'ttl_days', 60) persists to config."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting("activated_reaper", "ttl_days", 60)

        assert config_service.get_config().activated_reaper_config.ttl_days == 60

    def test_update_cadence_hours_persists(self, tmp_path):
        """update_setting('activated_reaper', 'cadence_hours', 12) persists to config."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting("activated_reaper", "cadence_hours", 12)

        assert config_service.get_config().activated_reaper_config.cadence_hours == 12

    def test_update_unknown_key_raises_value_error(self, tmp_path):
        """update_setting('activated_reaper', 'unknown_key', 99) raises ValueError."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))

        with pytest.raises(ValueError):
            config_service.update_setting("activated_reaper", "unknown_key", 99)
