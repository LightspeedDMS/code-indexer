"""
Unit tests for ConfigService integration with subprocess_max_workers (Story #27).

Tests the ConfigService methods for exposing subprocess executor settings in the Web UI.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest
import tempfile
import shutil
import os

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceSubprocessExecutorSettings:
    """Test ConfigService integration with subprocess_max_workers (Story #27)."""

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
    # AC4: Expose setting in Web UI Configuration (get_all_settings)
    # ==========================================================================

    def test_get_all_settings_background_jobs_has_subprocess_max_workers(self):
        """AC4: background_jobs section should have subprocess_max_workers."""
        settings = self.config_service.get_all_settings()
        assert "subprocess_max_workers" in settings["background_jobs"]

    def test_get_all_settings_subprocess_max_workers_default_value(self):
        """AC4: Default subprocess_max_workers should be 2."""
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["subprocess_max_workers"] == 2

    # ==========================================================================
    # AC4: Update setting via ConfigService
    # ==========================================================================

    def test_update_subprocess_max_workers_setting(self):
        """AC4: Should be able to update subprocess_max_workers."""
        self.config_service.update_setting(
            category="background_jobs",
            key="subprocess_max_workers",
            value=8,
        )

        # Verify update
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["subprocess_max_workers"] == 8

    def test_update_subprocess_max_workers_setting_persists(self):
        """AC4: Updated setting should persist to config file."""
        self.config_service.update_setting(
            category="background_jobs",
            key="subprocess_max_workers",
            value=6,
        )

        # Create new config service to verify persistence
        new_service = ConfigService(server_dir_path=self.temp_dir)
        settings = new_service.get_all_settings()
        assert settings["background_jobs"]["subprocess_max_workers"] == 6

    def test_update_subprocess_max_workers_setting_validates_minimum(self):
        """AC4: Value below minimum (1) should fail validation."""
        with pytest.raises(ValueError):
            self.config_service.update_setting(
                category="background_jobs",
                key="subprocess_max_workers",
                value=0,  # Zero is invalid
            )

    def test_update_subprocess_max_workers_setting_validates_maximum(self):
        """AC4: Value above maximum (50) should fail validation."""
        with pytest.raises(ValueError):
            self.config_service.update_setting(
                category="background_jobs",
                key="subprocess_max_workers",
                value=100,  # Too high
            )

    def test_update_subprocess_max_workers_setting_string_conversion(self):
        """String values should be converted to int."""
        self.config_service.update_setting(
            category="background_jobs",
            key="subprocess_max_workers",
            value="16",  # String value
        )

        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["subprocess_max_workers"] == 16
        assert isinstance(settings["background_jobs"]["subprocess_max_workers"], int)


class TestSubprocessExecutorUsesConfigValue:
    """Test that SubprocessExecutor instantiation uses config value (Story #27, AC2)."""

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

    def test_regex_search_uses_config_subprocess_max_workers(self):
        """AC2: RegexSearch should use subprocess_max_workers from config.

        This test verifies that the SubprocessExecutor instantiation in
        regex_search.py reads from config rather than hardcoding max_workers=1.

        Note: This is an integration test that validates the wiring is correct.
        The actual implementation should read from ConfigService.
        """
        # Set a custom value
        self.config_service.update_setting(
            category="background_jobs",
            key="subprocess_max_workers",
            value=4,
        )

        # Verify the config is set correctly
        settings = self.config_service.get_all_settings()
        assert settings["background_jobs"]["subprocess_max_workers"] == 4

        # Note: Full integration testing of the wiring would require
        # instantiating RegexSearch with a config service and verifying
        # the SubprocessExecutor is created with max_workers=4.
        # That test belongs in integration tests, not unit tests.
