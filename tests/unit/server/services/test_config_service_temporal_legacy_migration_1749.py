"""Unit tests for ConfigService's "temporal_legacy_migration" settings
category being correctly wired into get_all_settings() (Issue #1749).

Root cause: config_service.py defines `_temporal_legacy_migration_settings
(config)` (Issue #1548) but it was never invoked from `get_all_settings()`,
so the top-level `settings` dict never contained a "temporal_legacy_migration"
key. `routes.py`'s `settings.get("temporal_legacy_migration",
asdict(TemporalLegacyMigrationConfig()))` therefore silently fell back to the
compiled-in default (False/False) every time, regardless of the real
persisted `ServerConfig.temporal_legacy_migration_config` state. This is a
display-layer bug only -- the underlying config and the scheduler that reads
it directly are unaffected -- but it creates a real risk: the corresponding
update handler IS correctly wired, so an admin who saves the (wrongly
pre-filled "No") edit form without noticing will silently disable a
currently-working relocation job.

Mirrors `test_config_service_alias_lock_1546.py`'s established pattern for a
sibling settings section.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


class TestConfigServiceTemporalLegacyMigrationWiring:
    def test_get_all_settings_includes_temporal_legacy_migration_section(
        self, tmp_path
    ):
        """get_all_settings() must include a 'temporal_legacy_migration' key,
        mirroring every sibling settings section (fleet_migration, alias_lock,
        search_timeouts, etc.) -- before the fix this key was entirely absent
        because `_temporal_legacy_migration_settings()` was never called.
        """
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "temporal_legacy_migration" in settings, (
            "'temporal_legacy_migration' missing from get_all_settings() -- "
            "_temporal_legacy_migration_settings() is defined but never called."
        )

    @pytest.mark.parametrize("field_name", ["relocation_enabled", "cleanup_authorized"])
    def test_get_all_settings_reflects_persisted_field_true(self, tmp_path, field_name):
        """After persisting a field as True, get_all_settings() must report
        True -- not silently fall back to the dataclass default.

        This is the discriminating case: both fields default to False, so a
        test that never changes a field away from False could pass even on
        unwired/broken code. Only a genuine non-default persisted value
        proves the wiring is real.
        """
        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("temporal_legacy_migration", field_name, "true")

        settings = service.get_all_settings()
        assert settings.get("temporal_legacy_migration", {}).get(field_name) is True, (
            f"get_all_settings()['temporal_legacy_migration']['{field_name}'] "
            "does not reflect the persisted True value -- still silently "
            "defaulting to False."
        )
