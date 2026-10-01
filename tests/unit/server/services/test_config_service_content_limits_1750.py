"""Unit tests for ConfigService's "content_limits" settings category being
correctly wired into get_all_settings() (Issue #1750).

Root cause: `content_limits` is read by the admin config route
(`routes.py`'s `_get_current_config()`) via
`settings.get("content_limits", asdict(ContentLimitsConfig()))`, but
`get_all_settings()` (`config_service.py`) never produces a
"content_limits" key -- unlike the 9 other settings sections, there is no
`_content_limits_settings(config)` helper wired in. The admin Config
screen's Content Limits section therefore always displays compiled-in
defaults regardless of the real persisted `ServerConfig.content_limits_config`
state.

Mirrors `test_config_service_temporal_legacy_migration_1749.py`'s established
pattern for a sibling settings section (Issue #1749).
"""

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ContentLimitsConfig


class TestConfigServiceContentLimitsWiring:
    def test_get_all_settings_includes_content_limits_section(self, tmp_path):
        """get_all_settings() must include a 'content_limits' key, mirroring
        every sibling settings section (fleet_migration, alias_lock,
        temporal_legacy_migration, etc.) -- before the fix this key is
        entirely absent because `_content_limits_settings()` does not exist
        and is never called.
        """
        service = ConfigService(server_dir_path=str(tmp_path))
        settings = service.get_all_settings()
        assert "content_limits" in settings, (
            "'content_limits' missing from get_all_settings() -- "
            "_content_limits_settings() is not wired in."
        )

    @pytest.mark.parametrize(
        "field_name,non_default_value",
        [
            ("chars_per_token", 7),
            ("file_content_max_tokens", 12345),
            ("git_diff_max_tokens", 23456),
            ("git_log_max_tokens", 34567),
            ("search_result_max_tokens", 45678),
            ("cache_ttl_seconds", 9999),
        ],
    )
    def test_get_all_settings_reflects_persisted_field(
        self, tmp_path, field_name, non_default_value
    ):
        """After persisting a non-default field value and reloading via a
        FRESH ConfigService instance (proving real DB persistence, not just
        an in-memory mutation), get_all_settings() must report the real
        persisted value -- not silently fall back to the dataclass default.

        This is the discriminating case: a test that never changes a field
        away from its default could pass even on unwired/broken code (a
        `{}` fallback and the real default happen to look identical). Only a
        genuine non-default persisted value, round-tripped through a fresh
        instance, proves the wiring is real.
        """
        default_value = getattr(ContentLimitsConfig(), field_name)
        assert non_default_value != default_value, (
            "Test bug: chosen value is not actually non-default."
        )

        service = ConfigService(server_dir_path=str(tmp_path))
        config = service.load_config()
        assert config.content_limits_config is not None
        setattr(config.content_limits_config, field_name, non_default_value)
        service.save_config(config)

        # Fresh instance forces a real reload from the persisted store.
        fresh_service = ConfigService(server_dir_path=str(tmp_path))
        settings = fresh_service.get_all_settings()
        assert (
            settings.get("content_limits", {}).get(field_name) == non_default_value
        ), (
            f"get_all_settings()['content_limits']['{field_name}'] does not "
            "reflect the persisted non-default value -- still silently "
            "falling back to the compiled-in default."
        )
