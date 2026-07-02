"""
Unit tests for ConfigService data_retention handling (Story #400 - AC3, AC5, AC6).

Tests ConfigService.get_all_settings() serialization of data_retention section,
the _update_data_retention_setting() handler for each key,
and that cleanup_max_age_hours is absent from background_jobs section.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest

from code_indexer.server.utils.config_manager import ServerConfigManager


class TestConfigServiceDataRetentionSerialization:
    """AC3, AC6: ConfigService.get_all_settings() includes data_retention section."""

    def test_get_settings_includes_data_retention_section(self, tmp_path):
        """ConfigService.get_all_settings() should include data_retention section."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config_manager.save_config(config)

        config_service = ConfigService(str(tmp_path))
        settings = config_service.get_all_settings()

        assert "data_retention" in settings

    def test_get_settings_data_retention_has_all_fields(self, tmp_path):
        """ConfigService.get_all_settings() data_retention should include all 6 fields."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config_manager.save_config(config)

        config_service = ConfigService(str(tmp_path))
        settings = config_service.get_all_settings()
        dr = settings["data_retention"]

        assert "operational_logs_retention_hours" in dr
        assert "audit_logs_retention_hours" in dr
        assert "sync_jobs_retention_hours" in dr
        assert "dep_map_history_retention_hours" in dr
        assert "background_jobs_retention_hours" in dr
        assert "cleanup_interval_hours" in dr

    def test_get_settings_data_retention_default_values(self, tmp_path):
        """ConfigService.get_all_settings() data_retention should return correct defaults."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config_manager.save_config(config)

        config_service = ConfigService(str(tmp_path))
        settings = config_service.get_all_settings()
        dr = settings["data_retention"]

        assert dr["operational_logs_retention_hours"] == 168
        assert dr["audit_logs_retention_hours"] == 2160
        assert dr["sync_jobs_retention_hours"] == 720
        assert dr["dep_map_history_retention_hours"] == 2160
        assert dr["background_jobs_retention_hours"] == 720
        assert dr["cleanup_interval_hours"] == 1

    def test_background_jobs_settings_no_cleanup_max_age_hours(self, tmp_path):
        """AC5: ConfigService get_all_settings() background_jobs should NOT have cleanup_max_age_hours."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config_manager.save_config(config)

        config_service = ConfigService(str(tmp_path))
        settings = config_service.get_all_settings()

        assert "cleanup_max_age_hours" not in settings.get("background_jobs", {})


class TestConfigServiceDataRetentionUpdateHandler:
    """AC3: ConfigService update_setting() for data_retention category."""

    def test_update_operational_logs_retention_hours(self, tmp_path):
        """update_setting should update operational_logs_retention_hours."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting(
            "data_retention", "operational_logs_retention_hours", 336
        )

        assert (
            config_service.get_config().data_retention_config.operational_logs_retention_hours
            == 336
        )

    def test_update_audit_logs_retention_hours(self, tmp_path):
        """update_setting should update audit_logs_retention_hours."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting(
            "data_retention", "audit_logs_retention_hours", 4320
        )

        assert (
            config_service.get_config().data_retention_config.audit_logs_retention_hours
            == 4320
        )

    def test_update_sync_jobs_retention_hours(self, tmp_path):
        """update_setting should update sync_jobs_retention_hours."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting(
            "data_retention", "sync_jobs_retention_hours", 480
        )

        assert (
            config_service.get_config().data_retention_config.sync_jobs_retention_hours
            == 480
        )

    def test_update_dep_map_history_retention_hours(self, tmp_path):
        """update_setting should update dep_map_history_retention_hours."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting(
            "data_retention", "dep_map_history_retention_hours", 1080
        )

        assert (
            config_service.get_config().data_retention_config.dep_map_history_retention_hours
            == 1080
        )

    def test_update_background_jobs_retention_hours(self, tmp_path):
        """update_setting should update background_jobs_retention_hours."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting(
            "data_retention", "background_jobs_retention_hours", 360
        )

        assert (
            config_service.get_config().data_retention_config.background_jobs_retention_hours
            == 360
        )

    def test_update_cleanup_interval_hours(self, tmp_path):
        """update_setting should update cleanup_interval_hours."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting("data_retention", "cleanup_interval_hours", 6)

        assert (
            config_service.get_config().data_retention_config.cleanup_interval_hours
            == 6
        )

    def test_update_unknown_key_raises_value_error(self, tmp_path):
        """update_setting with unknown data_retention key should raise ValueError."""
        from code_indexer.server.services.config_service import ConfigService

        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())

        config_service = ConfigService(str(tmp_path))

        with pytest.raises(ValueError, match="Unknown data retention setting"):
            config_service.update_setting("data_retention", "nonexistent_key", 100)
