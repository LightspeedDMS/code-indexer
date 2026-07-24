"""Unit tests for Story #1457 (2026-07-24 re-review, Codex finding #4) --
config_service wiring of IndexingConfig.temporal_sister_relocation_enabled.

CLAUDE.md's absolute rule: "No Environment Variables for Server Settings --
runtime settings belong in the Web UI Config Screen via
get_config_service().get_config(), never os.environ directly." The AC1
relocation trigger's safety gate previously read
CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED directly from os.environ inside
server-side code -- this moves the on/off AUTHORITY to the config service;
the env var still transports the parent-resolved value into the temporal
child subprocess (same pattern as CIDX_SERVER_REFRESH_CONTEXT).

These tests assert the TARGET (post-fix) behavior and are expected to
currently FAIL (RED) since IndexingConfig has no
temporal_sister_relocation_enabled field yet and
_update_indexing_setting() does not recognize that key. Mirrors the Story
#1412 temporal_all_branches_enabled config_service wiring test pattern
(test_temporal_all_branches_gate_config_service_1412.py).
"""

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfigManager


def _make_service(tmp_path) -> ConfigService:
    mgr = ServerConfigManager(server_dir_path=str(tmp_path))
    return ConfigService(config_manager=mgr)


class TestGetConfigDisplaysTemporalSisterRelocationEnabled:
    def test_get_all_settings_includes_temporal_sister_relocation_enabled(
        self, tmp_path
    ) -> None:
        svc = _make_service(tmp_path)
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert "temporal_sister_relocation_enabled" in indexing, (
            "temporal_sister_relocation_enabled missing from "
            "get_all_settings()['indexing']"
        )

    def test_get_all_settings_default_is_false(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert indexing["temporal_sister_relocation_enabled"] is False, (
            "the safety gate must default OFF (2026-07-23 code review), "
            "byte-identical to pre-AC1 behavior"
        )


class TestUpdateIndexingSettingTemporalSisterRelocationEnabled:
    def test_update_true_string_sets_true(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc._update_indexing_setting("temporal_sister_relocation_enabled", "true")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_sister_relocation_enabled is True

    def test_update_false_string_sets_false(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc._update_indexing_setting("temporal_sister_relocation_enabled", "true")
        svc._update_indexing_setting("temporal_sister_relocation_enabled", "false")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_sister_relocation_enabled is False

    def test_update_persists_and_reflects_in_get_all_settings(self, tmp_path) -> None:
        svc = _make_service(tmp_path)
        svc._update_indexing_setting("temporal_sister_relocation_enabled", "true")
        settings = svc.get_all_settings()
        assert settings["indexing"]["temporal_sister_relocation_enabled"] is True
