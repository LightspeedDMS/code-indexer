"""
Unit tests for ConfigService service_display_name handling (Story #22).

Tests verify that:
1. get_all_settings includes service_display_name in server section
2. update_setting can change service_display_name
3. Empty display name handling falls back to default
4. Display name persists across service restarts

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceDisplayName:
    """Test suite for ConfigService service_display_name handling (Story #22)."""

    # ==========================================================================
    # AC1/AC4: get_all_settings includes display name
    # ==========================================================================

    def test_get_all_settings_includes_service_display_name(self, tmp_path):
        """AC4: get_all_settings should include service_display_name in server section."""
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()

        assert "server" in settings
        assert "service_display_name" in settings["server"]
        assert settings["server"]["service_display_name"] == "Neo"

    def test_get_all_settings_returns_custom_display_name(self, tmp_path):
        """Custom display name should be returned by get_all_settings."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Update display name
        service.update_setting("server", "service_display_name", "CustomBrand")

        # Verify get_all_settings returns the custom value
        settings = service.get_all_settings()
        assert settings["server"]["service_display_name"] == "CustomBrand"

    # ==========================================================================
    # AC2/AC4: update_setting for display name
    # ==========================================================================

    def test_update_service_display_name(self, tmp_path):
        """AC2/AC4: Should be able to update service_display_name via update_setting."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("server", "service_display_name", "MyServer")

        config = service.get_config()
        assert config.service_display_name == "MyServer"

    def test_update_service_display_name_persists(self, tmp_path):
        """AC4: Display name change should persist across service restarts."""
        # First service instance - set custom name
        service1 = ConfigService(server_dir_path=str(tmp_path))
        service1.load_config()
        service1.update_setting("server", "service_display_name", "PersistedName")

        # Second service instance - should load persisted name
        service2 = ConfigService(server_dir_path=str(tmp_path))
        config = service2.load_config()

        assert config.service_display_name == "PersistedName"

    # ==========================================================================
    # AC5: Empty display name handling
    # ==========================================================================

    def test_update_setting_with_empty_display_name(self, tmp_path):
        """AC5: Empty display name should be saved (fallback handled at usage site)."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        # Update with empty string
        service.update_setting("server", "service_display_name", "")

        config = service.get_config()
        # The raw value is empty, fallback to "Neo" at usage site
        assert config.service_display_name == ""

    def test_get_all_settings_with_empty_display_name(self, tmp_path):
        """get_all_settings should return empty string if display name is empty."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("server", "service_display_name", "")

        settings = service.get_all_settings()
        # Empty string stored, UI/protocol handles fallback
        assert settings["server"]["service_display_name"] == ""

    # ==========================================================================
    # Invalid category/key handling
    # ==========================================================================

    def test_update_unknown_server_setting_raises(self, tmp_path):
        """Unknown server setting key should raise ValueError."""
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        with pytest.raises(ValueError, match="Unknown server setting"):
            service.update_setting("server", "nonexistent_setting", "value")

    # ==========================================================================
    # Integration: Full workflow
    # ==========================================================================

    def test_display_name_full_workflow(self, tmp_path):
        """Full workflow: default -> custom -> persist -> reload."""
        # Step 1: Fresh install has default
        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.load_config()
        assert config.service_display_name == "Neo"

        # Step 2: Update to custom
        service.update_setting("server", "service_display_name", "ProductionServer")
        assert service.get_config().service_display_name == "ProductionServer"

        # Step 3: Verify in get_all_settings
        settings = service.get_all_settings()
        assert settings["server"]["service_display_name"] == "ProductionServer"

        # Step 4: Create new service, verify persistence
        new_service = ConfigService(server_dir_path=str(tmp_path))
        new_config = new_service.load_config()
        assert new_config.service_display_name == "ProductionServer"
