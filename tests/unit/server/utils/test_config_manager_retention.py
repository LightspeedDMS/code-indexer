"""
Unit tests for BackgroundJobsConfig cleanup_max_age_hours (Story #360).

Tests that the default cleanup_max_age_hours is 720 (30 days) instead of 24 hours.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

from code_indexer.server.utils.config_manager import BackgroundJobsConfig


class TestBackgroundJobsConfigRetentionDefault:
    """Tests for cleanup_max_age_hours default value (Story #360, Component 1)."""

    def test_default_cleanup_max_age_hours_is_720(self):
        """Story #360: Default cleanup_max_age_hours should be 720 (30 days)."""
        config = BackgroundJobsConfig()
        assert config.cleanup_max_age_hours == 720

    def test_custom_cleanup_max_age_hours(self):
        """Story #360: Custom cleanup_max_age_hours value should be accepted."""
        config = BackgroundJobsConfig(cleanup_max_age_hours=168)
        assert config.cleanup_max_age_hours == 168

    def test_minimum_cleanup_max_age_hours(self):
        """Story #360: Value of 1 should be accepted (minimum)."""
        config = BackgroundJobsConfig(cleanup_max_age_hours=1)
        assert config.cleanup_max_age_hours == 1

    def test_maximum_cleanup_max_age_hours(self):
        """Story #360: Value of 8760 should be accepted (1 year)."""
        config = BackgroundJobsConfig(cleanup_max_age_hours=8760)
        assert config.cleanup_max_age_hours == 8760

    def test_cleanup_max_age_hours_independent_of_other_fields(self):
        """Changing cleanup_max_age_hours should not affect other fields."""
        config = BackgroundJobsConfig(cleanup_max_age_hours=336)
        assert config.cleanup_max_age_hours == 336
        assert config.max_concurrent_background_jobs == 5
        assert config.subprocess_max_workers == 2
