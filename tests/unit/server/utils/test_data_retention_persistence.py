"""
Unit tests for DataRetentionConfig persistence and migration (Story #400 - AC3, AC5).

Tests ServerConfigManager save/load roundtrip for DataRetentionConfig,
and config migration from old BackgroundJobsConfig.cleanup_max_age_hours.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    DataRetentionConfig,
)


class TestDataRetentionConfigPersistence:
    """AC3: ServerConfigManager save/load for DataRetentionConfig."""

    def test_default_config_has_data_retention(self, tmp_path):
        """Fresh installation should have default DataRetentionConfig."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        assert config.data_retention_config is not None
        assert isinstance(config.data_retention_config, DataRetentionConfig)
        assert config.data_retention_config.background_jobs_retention_hours == 720

    def test_custom_data_retention_saves_to_file(self, tmp_path):
        """Custom data_retention settings should persist when saved to config file."""
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.data_retention_config.operational_logs_retention_hours = 336
        config.data_retention_config.background_jobs_retention_hours = 480
        config_manager.save_config(config)

        config_file = tmp_path / "config.json"
        with open(config_file) as f:
            saved_config = json.load(f)

        assert "data_retention_config" in saved_config
        dr = saved_config["data_retention_config"]
        assert dr["operational_logs_retention_hours"] == 336
        assert dr["background_jobs_retention_hours"] == 480

    def test_custom_data_retention_loads_from_file(self, tmp_path):
        """Custom data_retention settings should load correctly from config file."""
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "data_retention_config": {
                "operational_logs_retention_hours": 336,
                "audit_logs_retention_hours": 4320,
                "sync_jobs_retention_hours": 480,
                "dep_map_history_retention_hours": 1440,
                "background_jobs_retention_hours": 360,
                "cleanup_interval_hours": 4,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        dr = config.data_retention_config
        assert dr.operational_logs_retention_hours == 336
        assert dr.audit_logs_retention_hours == 4320
        assert dr.sync_jobs_retention_hours == 480
        assert dr.dep_map_history_retention_hours == 1440
        assert dr.background_jobs_retention_hours == 360
        assert dr.cleanup_interval_hours == 4

    def test_config_without_data_retention_gets_defaults(self, tmp_path):
        """Old config without data_retention_config should get defaults on load."""
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config.data_retention_config is not None
        assert config.data_retention_config.background_jobs_retention_hours == 720
        assert config.data_retention_config.operational_logs_retention_hours == 168

    def test_config_roundtrip_preserves_data_retention_settings(self, tmp_path):
        """Data retention settings should survive save/load roundtrip."""
        config_manager = ServerConfigManager(str(tmp_path))

        config = config_manager.create_default_config()
        config.data_retention_config.audit_logs_retention_hours = 4320
        config.data_retention_config.cleanup_interval_hours = 12
        config_manager.save_config(config)

        loaded_config = config_manager.load_config()
        assert loaded_config.data_retention_config.audit_logs_retention_hours == 4320
        assert loaded_config.data_retention_config.cleanup_interval_hours == 12


class TestDataRetentionConfigMigration:
    """AC5: Migration from old BackgroundJobsConfig.cleanup_max_age_hours."""

    def test_migration_carries_cleanup_max_age_hours_to_background_jobs_retention(
        self, tmp_path
    ):
        """Old config with cleanup_max_age_hours should migrate to background_jobs_retention_hours."""
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "background_jobs_config": {
                "max_concurrent_background_jobs": 5,
                "subprocess_max_workers": 2,
                "cleanup_max_age_hours": 480,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config.data_retention_config is not None
        assert config.data_retention_config.background_jobs_retention_hours == 480

    def test_migration_does_not_override_explicit_data_retention_config(self, tmp_path):
        """If both old and new configs exist, data_retention_config takes precedence."""
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "background_jobs_config": {
                "max_concurrent_background_jobs": 5,
                "subprocess_max_workers": 2,
                "cleanup_max_age_hours": 480,
            },
            "data_retention_config": {
                "background_jobs_retention_hours": 360,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        # Explicit data_retention_config should take precedence
        assert config.data_retention_config.background_jobs_retention_hours == 360

    def test_migration_uses_default_when_no_old_cleanup_value(self, tmp_path):
        """Without old cleanup_max_age_hours, default 720 should apply."""
        config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "background_jobs_config": {
                "max_concurrent_background_jobs": 5,
                "subprocess_max_workers": 2,
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(config_data, f)

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        assert config.data_retention_config.background_jobs_retention_hours == 720
