"""
Unit tests for DataRetentionConfig dataclass (Story #400 - AC1, AC5).

Tests the new DataRetentionConfig dataclass defaults, field values,
and that BackgroundJobsConfig.cleanup_max_age_hours is removed.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

from code_indexer.server.utils.config_manager import (
    DataRetentionConfig,
    BackgroundJobsConfig,
    ServerConfig,
)


class TestDataRetentionConfigDefaults:
    """AC1: DataRetentionConfig dataclass default values."""

    def test_default_operational_logs_retention_hours(self):
        """Default operational_logs_retention_hours should be 168 (7 days)."""
        config = DataRetentionConfig()
        assert config.operational_logs_retention_hours == 168

    def test_default_audit_logs_retention_hours(self):
        """Default audit_logs_retention_hours should be 2160 (90 days)."""
        config = DataRetentionConfig()
        assert config.audit_logs_retention_hours == 2160

    def test_default_sync_jobs_retention_hours(self):
        """Default sync_jobs_retention_hours should be 720 (30 days)."""
        config = DataRetentionConfig()
        assert config.sync_jobs_retention_hours == 720

    def test_default_dep_map_history_retention_hours(self):
        """Default dep_map_history_retention_hours should be 2160 (90 days)."""
        config = DataRetentionConfig()
        assert config.dep_map_history_retention_hours == 2160

    def test_default_background_jobs_retention_hours(self):
        """Default background_jobs_retention_hours should be 720 (30 days)."""
        config = DataRetentionConfig()
        assert config.background_jobs_retention_hours == 720

    def test_default_cleanup_interval_hours(self):
        """Default cleanup_interval_hours should be 1."""
        config = DataRetentionConfig()
        assert config.cleanup_interval_hours == 1


class TestDataRetentionConfigCustomValues:
    """AC1: DataRetentionConfig accepts custom values."""

    def test_custom_values_initialization(self):
        """All fields can be set to custom values during initialization."""
        config = DataRetentionConfig(
            operational_logs_retention_hours=48,
            audit_logs_retention_hours=4320,
            sync_jobs_retention_hours=360,
            dep_map_history_retention_hours=1080,
            background_jobs_retention_hours=480,
            cleanup_interval_hours=6,
        )
        assert config.operational_logs_retention_hours == 48
        assert config.audit_logs_retention_hours == 4320
        assert config.sync_jobs_retention_hours == 360
        assert config.dep_map_history_retention_hours == 1080
        assert config.background_jobs_retention_hours == 480
        assert config.cleanup_interval_hours == 6

    def test_minimum_retention_value_one(self):
        """Minimum retention value of 1 should be allowed."""
        config = DataRetentionConfig(operational_logs_retention_hours=1)
        assert config.operational_logs_retention_hours == 1

    def test_maximum_retention_value_8760(self):
        """Maximum retention value of 8760 (1 year) should be allowed."""
        config = DataRetentionConfig(operational_logs_retention_hours=8760)
        assert config.operational_logs_retention_hours == 8760

    def test_minimum_cleanup_interval_one(self):
        """Minimum cleanup_interval_hours of 1 should be allowed."""
        config = DataRetentionConfig(cleanup_interval_hours=1)
        assert config.cleanup_interval_hours == 1

    def test_maximum_cleanup_interval_24(self):
        """Maximum cleanup_interval_hours of 24 should be allowed."""
        config = DataRetentionConfig(cleanup_interval_hours=24)
        assert config.cleanup_interval_hours == 24


class TestServerConfigDataRetentionField:
    """AC1: ServerConfig includes data_retention_config field."""

    def test_server_config_has_data_retention_config_field(self):
        """ServerConfig should have data_retention_config field."""
        config = ServerConfig(server_dir="/tmp/test")
        assert hasattr(config, "data_retention_config")

    def test_server_config_initializes_data_retention_config(self):
        """ServerConfig should auto-initialize data_retention_config in __post_init__."""
        config = ServerConfig(server_dir="/tmp/test")
        assert config.data_retention_config is not None
        assert isinstance(config.data_retention_config, DataRetentionConfig)

    def test_server_config_data_retention_defaults(self):
        """ServerConfig should have default DataRetentionConfig values."""
        config = ServerConfig(server_dir="/tmp/test")
        dr = config.data_retention_config
        assert dr.operational_logs_retention_hours == 168
        assert dr.audit_logs_retention_hours == 2160
        assert dr.sync_jobs_retention_hours == 720
        assert dr.dep_map_history_retention_hours == 2160
        assert dr.background_jobs_retention_hours == 720
        assert dr.cleanup_interval_hours == 1


class TestBackgroundJobsConfigCleanupFieldRemoved:
    """AC5: cleanup_max_age_hours removed from BackgroundJobsConfig."""

    def test_background_jobs_config_has_no_cleanup_max_age_hours(self):
        """BackgroundJobsConfig should NOT have cleanup_max_age_hours field."""
        config = BackgroundJobsConfig()
        assert not hasattr(config, "cleanup_max_age_hours"), (
            "cleanup_max_age_hours should be removed from BackgroundJobsConfig "
            "and moved to DataRetentionConfig.background_jobs_retention_hours"
        )

    def test_background_jobs_config_still_has_required_fields(self):
        """BackgroundJobsConfig should still have max_concurrent_background_jobs and subprocess_max_workers."""
        config = BackgroundJobsConfig()
        assert hasattr(config, "max_concurrent_background_jobs")
        assert hasattr(config, "subprocess_max_workers")
        assert config.max_concurrent_background_jobs == 5
        assert config.subprocess_max_workers == 2
