"""
Unit tests for ConfigService integration with cleanup_max_age_hours (Story #360).

Tests the ConfigService methods for exposing and updating the job history
retention setting (cleanup_max_age_hours) in the Web UI.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest
import tempfile
import shutil
import os

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceRetentionSettings:
    """Test ConfigService integration with cleanup_max_age_hours (Story #360)."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        # Initialize config
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up test environment."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    # ==========================================================================
    # Component 2: Expose in get_all_settings()
    # ==========================================================================

    def test_get_all_settings_includes_cleanup_max_age_hours(self):
        """Story #360: get_all_settings should include cleanup_max_age_hours in background_jobs."""
        settings = self.config_service.get_all_settings()
        assert "background_jobs" in settings
        assert "cleanup_max_age_hours" in settings["background_jobs"]

    def test_get_all_settings_cleanup_max_age_hours_default_value(self):
        """Story #360: Default cleanup_max_age_hours should be 720 (30 days)."""
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["cleanup_max_age_hours"] == 720

    # ==========================================================================
    # Component 3: Update handler for cleanup_max_age_hours
    # ==========================================================================

    def test_update_background_jobs_cleanup_max_age_hours(self):
        """Story #360: Should be able to update cleanup_max_age_hours via update_setting."""
        self.config_service.update_setting(
            category="background_jobs",
            key="cleanup_max_age_hours",
            value=168,
        )

        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["cleanup_max_age_hours"] == 168

    def test_update_background_jobs_cleanup_max_age_hours_persists(self):
        """Story #360: Updated cleanup_max_age_hours should persist to config file."""
        self.config_service.update_setting(
            category="background_jobs",
            key="cleanup_max_age_hours",
            value=336,
        )

        # Create new config service instance to verify persistence
        new_service = ConfigService(server_dir_path=self.temp_dir)
        settings = new_service.get_all_settings()
        assert settings["background_jobs"]["cleanup_max_age_hours"] == 336

    def test_update_background_jobs_cleanup_max_age_hours_string_conversion(self):
        """String values should be converted to int."""
        self.config_service.update_setting(
            category="background_jobs",
            key="cleanup_max_age_hours",
            value="480",
        )

        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["cleanup_max_age_hours"] == 480
        assert isinstance(settings["background_jobs"]["cleanup_max_age_hours"], int)

    def test_update_background_jobs_unknown_key_raises(self):
        """Unknown keys in background_jobs category should still raise ValueError."""
        with pytest.raises(ValueError, match="Unknown background jobs setting"):
            self.config_service.update_setting(
                category="background_jobs",
                key="nonexistent_retention_key",
                value=42,
            )

    def test_existing_background_jobs_keys_still_work(self):
        """Existing background_jobs keys should still be updatable after adding cleanup_max_age_hours."""
        # Verify existing keys are unaffected
        self.config_service.update_setting(
            category="background_jobs",
            key="max_concurrent_background_jobs",
            value=10,
        )
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["max_concurrent_background_jobs"] == 10

        self.config_service.update_setting(
            category="background_jobs",
            key="subprocess_max_workers",
            value=4,
        )
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["subprocess_max_workers"] == 4
