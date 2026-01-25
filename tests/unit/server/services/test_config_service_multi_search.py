"""
Unit tests for ConfigService multi_search section handling (Story #25).

Tests verify that:
1. get_all_settings includes multi_search section with all fields
2. update_setting can change multi_search fields
3. Settings persist across service restarts
4. Invalid values are rejected

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceMultiSearchGetAllSettings:
    """Test suite for ConfigService get_all_settings multi_search section (Story #25)."""

    # ==========================================================================
    # AC1: get_all_settings includes multi_search section
    # ==========================================================================

    def test_get_all_settings_includes_multi_search_section(self, tmp_path):
        """AC1: get_all_settings should include multi_search section."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert "multi_search" in settings

    def test_get_all_settings_multi_search_has_all_fields(self, tmp_path):
        """AC1: multi_search section should have all expected fields."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        multi_search = settings["multi_search"]
        assert "multi_search_max_workers" in multi_search
        assert "multi_search_timeout_seconds" in multi_search
        assert "scip_multi_max_workers" in multi_search
        assert "scip_multi_timeout_seconds" in multi_search

    def test_get_all_settings_multi_search_default_values(self, tmp_path):
        """AC1: multi_search section should have correct default values."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        multi_search = settings["multi_search"]
        # Per resource audit: defaults should be 2 workers, 30s timeout
        assert multi_search["multi_search_max_workers"] == 2
        assert multi_search["multi_search_timeout_seconds"] == 30
        assert multi_search["scip_multi_max_workers"] == 2
        assert multi_search["scip_multi_timeout_seconds"] == 30

    def test_get_all_settings_returns_custom_multi_search_values(self, tmp_path):
        """Custom multi_search values should be returned by get_all_settings."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Update multi_search settings
        service.update_setting("multi_search", "multi_search_max_workers", 5)
        service.update_setting("multi_search", "scip_multi_timeout_seconds", 60)

        # Verify get_all_settings returns the custom values
        settings = service.get_all_settings()
        assert settings["multi_search"]["multi_search_max_workers"] == 5
        assert settings["multi_search"]["scip_multi_timeout_seconds"] == 60


class TestConfigServiceMultiSearchUpdateSetting:
    """Test suite for ConfigService update_setting multi_search category (Story #25)."""

    # ==========================================================================
    # AC2: update_setting for multi_search fields
    # ==========================================================================

    def test_update_multi_search_max_workers(self, tmp_path):
        """AC2: Should be able to update multi_search_max_workers via update_setting."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("multi_search", "multi_search_max_workers", 8)

        config = service.get_config()
        assert config.multi_search_limits_config.multi_search_max_workers == 8

    def test_update_multi_search_timeout_seconds(self, tmp_path):
        """AC2: Should be able to update multi_search_timeout_seconds via update_setting."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("multi_search", "multi_search_timeout_seconds", 60)

        config = service.get_config()
        assert config.multi_search_limits_config.multi_search_timeout_seconds == 60

    def test_update_scip_multi_max_workers(self, tmp_path):
        """AC2: Should be able to update scip_multi_max_workers via update_setting."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("multi_search", "scip_multi_max_workers", 4)

        config = service.get_config()
        assert config.multi_search_limits_config.scip_multi_max_workers == 4

    def test_update_scip_multi_timeout_seconds(self, tmp_path):
        """AC2: Should be able to update scip_multi_timeout_seconds via update_setting."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("multi_search", "scip_multi_timeout_seconds", 45)

        config = service.get_config()
        assert config.multi_search_limits_config.scip_multi_timeout_seconds == 45

    def test_update_multi_search_setting_persists(self, tmp_path):
        """AC2: Multi_search setting change should persist across service restarts."""
        # First service instance - set custom value
        service1 = ConfigService(server_dir_path=str(tmp_path))
        service1.load_config()
        service1.update_setting("multi_search", "multi_search_max_workers", 10)

        # Second service instance - should load persisted value
        service2 = ConfigService(server_dir_path=str(tmp_path))
        config = service2.load_config()

        assert config.multi_search_limits_config.multi_search_max_workers == 10

    # ==========================================================================
    # Error handling tests
    # ==========================================================================

    def test_update_unknown_multi_search_setting_raises(self, tmp_path):
        """Unknown multi_search setting key should raise ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="Unknown multi_search setting"):
            service.update_setting("multi_search", "nonexistent_setting", 5)

    def test_update_multi_search_max_workers_validates(self, tmp_path):
        """Invalid multi_search_max_workers should fail validation."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Zero workers should fail validation
        with pytest.raises(ValueError, match="multi_search_max_workers"):
            service.update_setting("multi_search", "multi_search_max_workers", 0)

    def test_update_multi_search_timeout_validates(self, tmp_path):
        """Invalid multi_search_timeout_seconds should fail validation."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Too low timeout should fail validation (minimum is 5)
        with pytest.raises(ValueError, match="multi_search_timeout_seconds"):
            service.update_setting("multi_search", "multi_search_timeout_seconds", 2)


class TestConfigServiceMultiSearchFullWorkflow:
    """Test suite for ConfigService multi_search full workflow (Story #25)."""

    # ==========================================================================
    # Integration: Full workflow
    # ==========================================================================

    def test_multi_search_full_workflow(self, tmp_path):
        """Full workflow: default -> custom -> persist -> reload."""
        # Step 1: Fresh install has default values
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.load_config()
        assert config.multi_search_limits_config.multi_search_max_workers == 2
        assert config.multi_search_limits_config.scip_multi_max_workers == 2

        # Step 2: Update to custom values
        service.update_setting("multi_search", "multi_search_max_workers", 6)
        service.update_setting("multi_search", "multi_search_timeout_seconds", 90)
        service.update_setting("multi_search", "scip_multi_max_workers", 4)
        service.update_setting("multi_search", "scip_multi_timeout_seconds", 60)

        # Step 3: Verify in get_all_settings
        settings = service.get_all_settings()
        assert settings["multi_search"]["multi_search_max_workers"] == 6
        assert settings["multi_search"]["multi_search_timeout_seconds"] == 90
        assert settings["multi_search"]["scip_multi_max_workers"] == 4
        assert settings["multi_search"]["scip_multi_timeout_seconds"] == 60

        # Step 4: Create new service, verify persistence
        new_service = ConfigService(server_dir_path=str(tmp_path))
        new_config = new_service.load_config()
        assert new_config.multi_search_limits_config.multi_search_max_workers == 6
        assert new_config.multi_search_limits_config.multi_search_timeout_seconds == 90
        assert new_config.multi_search_limits_config.scip_multi_max_workers == 4
        assert new_config.multi_search_limits_config.scip_multi_timeout_seconds == 60
