"""Tests for ConfigService wiring of IndexingWatchdogConfig (Issue #1530).

Mirrors the get_all_settings() / update_setting() wiring pattern established
for hnsw_orphan_sweep / search_timeouts: an "indexing_watchdog" category
dict read helper wired into get_all_settings(), and an
"elif category == 'indexing_watchdog':" branch in update_setting()
dispatching to an _update_indexing_watchdog_setting() write helper.
"""

from code_indexer.server.services.config_service import ConfigService


def _make_service(tmp_path) -> ConfigService:
    return ConfigService(server_dir_path=str(tmp_path))


class TestGetAllSettingsSurfacesIndexingWatchdog:
    def test_section_key_present_with_default(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        settings = svc.get_all_settings()
        assert "indexing_watchdog" in settings
        assert settings["indexing_watchdog"]["stale_activity_timeout_seconds"] == 120.0


class TestUpdateSettingIndexingWatchdog:
    def test_update_stale_activity_timeout_seconds(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc.update_setting("indexing_watchdog", "stale_activity_timeout_seconds", 90.0)
        assert (
            svc.get_config().indexing_watchdog_config.stale_activity_timeout_seconds
            == 90.0
        )
