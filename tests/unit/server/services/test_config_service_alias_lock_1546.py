"""Unit tests for ConfigService's "alias_lock" settings category
(Issue #1546).

`alias_lock.db_backed_enabled` is the operator-controlled rollout gate for
the DB-backed golden-repo alias lock -- default ON as of Phase 3 (proven
correct on a live 3-node staging cluster). The flag remains available as
an emergency rollback to the old file-based WriteLockManager mechanism,
mirroring the `fleet_migration.enabled` pattern exactly.
"""

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceAliasLockSetting:
    def test_get_all_settings_includes_alias_lock_section(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "alias_lock" in settings
        assert settings["alias_lock"]["db_backed_enabled"] is True

    def test_update_db_backed_enabled_to_true_persists(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        service.update_setting("alias_lock", "db_backed_enabled", "true")

        settings = service.get_all_settings()
        assert settings["alias_lock"]["db_backed_enabled"] is True

    def test_unknown_alias_lock_key_raises(self, tmp_path):
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        try:
            service.update_setting("alias_lock", "not_a_real_key", "true")
            assert False, "expected ValueError for unknown alias_lock key"
        except ValueError:
            pass
