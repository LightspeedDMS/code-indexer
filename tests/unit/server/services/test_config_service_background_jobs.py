"""
Unit tests for ConfigService integration with BackgroundJobsConfig (Story #26).

Tests the ConfigService methods for exposing background jobs settings in the Web UI.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest
import tempfile
from pathlib import Path

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceBackgroundJobsSettings:
    """Test ConfigService integration with BackgroundJobsConfig (Story #26)."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        # Initialize config
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up test environment."""
        import shutil
        import os
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    # ==========================================================================
    # AC4: Expose setting in Web UI Configuration (get_all_settings)
    # ==========================================================================

    def test_get_all_settings_includes_background_jobs_section(self):
        """AC4: get_all_settings should include background_jobs section."""
        settings = self.config_service.get_all_settings()
        assert "background_jobs" in settings

    def test_get_all_settings_background_jobs_has_max_concurrent(self):
        """AC4: background_jobs section should have max_concurrent_background_jobs."""
        settings = self.config_service.get_all_settings()
        assert "max_concurrent_background_jobs" in settings["background_jobs"]

    def test_get_all_settings_background_jobs_default_value(self):
        """AC4: Default max_concurrent_background_jobs should be 5."""
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["max_concurrent_background_jobs"] == 5

    # ==========================================================================
    # AC4: Update setting via ConfigService
    # ==========================================================================

    def test_update_background_jobs_setting(self):
        """AC4: Should be able to update max_concurrent_background_jobs."""
        self.config_service.update_setting(
            category="background_jobs",
            key="max_concurrent_background_jobs",
            value=10,
        )

        # Verify update
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["max_concurrent_background_jobs"] == 10

    def test_update_background_jobs_setting_persists(self):
        """AC4: Updated setting should persist to config file."""
        self.config_service.update_setting(
            category="background_jobs",
            key="max_concurrent_background_jobs",
            value=8,
        )

        # Create new config service to verify persistence
        new_service = ConfigService(server_dir_path=self.temp_dir)
        settings = new_service.get_all_settings()
        assert settings["background_jobs"]["max_concurrent_background_jobs"] == 8

    def test_update_background_jobs_setting_validates(self):
        """AC4: Invalid values should fail validation."""
        with pytest.raises(ValueError):
            self.config_service.update_setting(
                category="background_jobs",
                key="max_concurrent_background_jobs",
                value=0,  # Zero is invalid
            )

    def test_update_background_jobs_unknown_key_raises(self):
        """Unknown key in background_jobs category should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown background jobs setting"):
            self.config_service.update_setting(
                category="background_jobs",
                key="nonexistent_key",
                value=5,
            )

    def test_update_background_jobs_setting_string_conversion(self):
        """String values should be converted to int."""
        self.config_service.update_setting(
            category="background_jobs",
            key="max_concurrent_background_jobs",
            value="12",  # String value
        )

        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["max_concurrent_background_jobs"] == 12
        assert isinstance(settings["background_jobs"]["max_concurrent_background_jobs"], int)
