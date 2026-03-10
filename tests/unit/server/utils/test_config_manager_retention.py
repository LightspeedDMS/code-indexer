"""
Unit tests for background job retention configuration (Story #360, Story #400).

Story #360 established configurable job history retention (was in BackgroundJobsConfig).
Story #400 moved retention to DataRetentionConfig.background_jobs_retention_hours.

These tests verify the current location of the retention field.
"""

from code_indexer.server.utils.config_manager import DataRetentionConfig


class TestBackgroundJobsRetentionInDataRetentionConfig:
    """Tests for background_jobs_retention_hours in DataRetentionConfig (Story #400 - AC5)."""

    def test_default_background_jobs_retention_hours_is_720(self):
        """Story #400: Default background_jobs_retention_hours should be 720 (30 days)."""
        config = DataRetentionConfig()
        assert config.background_jobs_retention_hours == 720

    def test_custom_background_jobs_retention_hours(self):
        """Story #400: Custom background_jobs_retention_hours value should be accepted."""
        config = DataRetentionConfig(background_jobs_retention_hours=168)
        assert config.background_jobs_retention_hours == 168

    def test_minimum_background_jobs_retention_hours(self):
        """Story #400: Value of 1 should be accepted (minimum)."""
        config = DataRetentionConfig(background_jobs_retention_hours=1)
        assert config.background_jobs_retention_hours == 1

    def test_maximum_background_jobs_retention_hours(self):
        """Story #400: Value of 8760 should be accepted (1 year)."""
        config = DataRetentionConfig(background_jobs_retention_hours=8760)
        assert config.background_jobs_retention_hours == 8760

    def test_background_jobs_retention_independent_of_other_fields(self):
        """Changing background_jobs_retention_hours should not affect other fields."""
        config = DataRetentionConfig(background_jobs_retention_hours=336)
        assert config.background_jobs_retention_hours == 336
        assert config.operational_logs_retention_hours == 168
        assert config.audit_logs_retention_hours == 2160
        assert config.sync_jobs_retention_hours == 720
        assert config.dep_map_history_retention_hours == 2160
        assert config.cleanup_interval_hours == 1
