"""
Unit tests for ConfigService golden_repos.externally_managed (EVO-64493).

Verifies the flag is exposed in get_all_settings() and can be written via
update_setting (bool and string forms), mirroring the description_refresh_enabled
boolean-setting precedent.
"""

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceExternallyManaged:
    def test_get_all_settings_includes_externally_managed(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "golden_repos" in settings
        assert "externally_managed" in settings["golden_repos"]

    def test_externally_managed_default_value(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert isinstance(settings["golden_repos"]["externally_managed"], bool)
        assert settings["golden_repos"]["externally_managed"] is False

    def test_update_externally_managed_true(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("golden_repos", "externally_managed", True)
        settings = service.get_all_settings()
        assert settings["golden_repos"]["externally_managed"] is True

    def test_update_externally_managed_false(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("golden_repos", "externally_managed", False)
        settings = service.get_all_settings()
        assert settings["golden_repos"]["externally_managed"] is False

    def test_update_externally_managed_string_true(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("golden_repos", "externally_managed", "true")
        settings = service.get_all_settings()
        assert settings["golden_repos"]["externally_managed"] is True

    def test_update_externally_managed_persists(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("golden_repos", "externally_managed", True)
        # Re-read from a fresh service to confirm it persisted to disk.
        reloaded = ConfigService(server_dir_path=str(tmp_path))
        settings = reloaded.get_all_settings()
        assert settings["golden_repos"]["externally_managed"] is True
