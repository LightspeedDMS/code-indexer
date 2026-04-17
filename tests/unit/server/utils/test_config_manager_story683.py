"""
Unit tests for Story #683: CIDX Server Config Cleanup Part 2 (AC1-AC3, AC5).

Tests:
- AC1: loader guards strip indexing_timeout_seconds and temporal_stale_threshold_days
       from indexing_config (duplicate fields removed, ScipConfig remains canonical)
- AC2: loader guard strips csrf_max_age_seconds from web_security_config (dead field)
- AC3: AuthConfig removed from ServerConfig; auth_config added to EXPECTED_ORPHAN_KEYS
- AC5: job_queue removed from valid_sections, _validate_config_section, and
       _get_current_config (fabricated section fully deleted)
"""

import json
import logging

import pytest

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    EXPECTED_ORPHAN_KEYS,
)

# Constants for legacy field values injected to simulate upgrade scenarios
_LEGACY_INDEXING_TIMEOUT = 9999
_LEGACY_STALE_THRESHOLD = 999
_LEGACY_CSRF_MAX_AGE = 1234
_LEGACY_OAUTH_THRESHOLD_HOURS = 4


@pytest.fixture
def default_config(tmp_path):
    """Return a fresh default ServerConfig for inspection tests."""
    manager = ServerConfigManager(str(tmp_path))
    return manager.create_default_config()


def _mutate_and_reload(tmp_path, mutation_fn):
    """Create, save, apply mutation_fn to the raw JSON dict, then reload.

    Returns the loaded ServerConfig.
    """
    manager = ServerConfigManager(str(tmp_path))
    manager.save_config(manager.create_default_config())
    config_path = tmp_path / "config.json"
    raw = json.loads(config_path.read_text())
    mutation_fn(raw)
    config_path.write_text(json.dumps(raw))
    return manager.load_config()


class TestAC1LoaderGuards:
    """AC1: Loader strips indexing_config duplicate fields on load."""

    def test_strips_indexing_timeout_seconds(self, tmp_path):
        """indexing_timeout_seconds injected into indexing_config must be stripped."""
        loaded = _mutate_and_reload(
            tmp_path,
            lambda raw: raw["indexing_config"].update(
                {"indexing_timeout_seconds": _LEGACY_INDEXING_TIMEOUT}
            ),
        )
        assert loaded is not None
        assert not hasattr(loaded.indexing_config, "indexing_timeout_seconds")

    def test_strips_temporal_stale_threshold_days(self, tmp_path):
        """temporal_stale_threshold_days injected into indexing_config must be stripped."""
        loaded = _mutate_and_reload(
            tmp_path,
            lambda raw: raw["indexing_config"].update(
                {"temporal_stale_threshold_days": _LEGACY_STALE_THRESHOLD}
            ),
        )
        assert loaded is not None
        assert not hasattr(loaded.indexing_config, "temporal_stale_threshold_days")

    def test_indexing_config_retains_indexable_extensions(self, default_config):
        """IndexingConfig.indexable_extensions must remain after field removal."""
        assert default_config.indexing_config is not None
        assert hasattr(default_config.indexing_config, "indexable_extensions")
        assert len(default_config.indexing_config.indexable_extensions) > 0


class TestAC1CanonicalFields:
    """AC1: ScipConfig retains canonical fields removed from IndexingConfig."""

    def test_scip_config_retains_indexing_timeout_seconds(self, default_config):
        """ScipConfig.indexing_timeout_seconds must remain as canonical field."""
        assert default_config.scip_config is not None
        assert hasattr(default_config.scip_config, "indexing_timeout_seconds")

    def test_scip_config_retains_temporal_stale_threshold_days(self, default_config):
        """ScipConfig.temporal_stale_threshold_days must remain as canonical field."""
        assert default_config.scip_config is not None
        assert hasattr(default_config.scip_config, "temporal_stale_threshold_days")


class TestAC2:
    """AC2: Loader guard strips csrf_max_age_seconds from web_security_config."""

    def test_strips_csrf_max_age_seconds(self, tmp_path):
        """csrf_max_age_seconds injected into web_security_config must be stripped."""
        loaded = _mutate_and_reload(
            tmp_path,
            lambda raw: raw["web_security_config"].update(
                {"csrf_max_age_seconds": _LEGACY_CSRF_MAX_AGE}
            ),
        )
        assert loaded is not None
        assert not hasattr(loaded.web_security_config, "csrf_max_age_seconds")

    def test_web_security_config_retains_session_timeouts(self, default_config):
        """WebSecurityConfig must retain both session timeout fields."""
        assert default_config.web_security_config is not None
        assert hasattr(
            default_config.web_security_config, "web_session_timeout_seconds"
        )
        assert hasattr(
            default_config.web_security_config, "admin_session_timeout_seconds"
        )


class TestAC3:
    """AC3: AuthConfig removed from ServerConfig; auth_config in EXPECTED_ORPHAN_KEYS."""

    def test_auth_config_in_expected_orphan_keys(self):
        """auth_config must be in EXPECTED_ORPHAN_KEYS."""
        assert "auth_config" in EXPECTED_ORPHAN_KEYS

    def test_server_config_has_no_auth_config_field(self, default_config):
        """ServerConfig must not have auth_config field after AC3 removal."""
        assert not hasattr(default_config, "auth_config")

    def test_loader_strips_auth_config_without_warning(self, tmp_path, caplog):
        """Loading config with old auth_config key must not emit WARNING."""
        with caplog.at_level(
            logging.DEBUG, logger="code_indexer.server.utils.config_manager"
        ):
            loaded = _mutate_and_reload(
                tmp_path,
                lambda raw: raw.update(
                    {
                        "auth_config": {
                            "oauth_extension_threshold_hours": _LEGACY_OAUTH_THRESHOLD_HOURS
                        }
                    }
                ),
            )

        assert loaded is not None
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "auth_config" in r.message
        ]
        assert len(warning_records) == 0


class TestAC5:
    """AC5: job_queue fabricated section fully removed from routes.py."""

    def test_valid_sections_excludes_job_queue(self):
        """_VALID_CONFIG_SECTIONS constant must not contain job_queue after AC5."""
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "job_queue" not in _VALID_CONFIG_SECTIONS

    def test_validate_config_section_job_queue_returns_none(self):
        """_validate_config_section must return None for job_queue (no handler)."""
        from code_indexer.server.web.routes import _validate_config_section

        result = _validate_config_section(
            "job_queue",
            {
                "max_total_concurrent_jobs": 5,
                "max_concurrent_jobs_per_user": 2,
                "average_job_duration_minutes": 10,
            },
        )
        assert result is None

    def test_get_current_config_excludes_job_queue(self):
        """_get_current_config must not emit a job_queue key after AC5."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.web.routes import _get_current_config

        minimal_settings = {
            "server": {},
            "cache": {},
            "timeouts": {},
            "password_security": {},
            "oidc": {},
        }
        mock_service = MagicMock()
        mock_service.get_all_settings.return_value = minimal_settings
        with patch(
            "code_indexer.server.web.routes.get_config_service",
            return_value=mock_service,
        ):
            config = _get_current_config()

        assert "job_queue" not in config


# Only cidx_scip_generate_timeout was truly removed in AC7 (uses ScipConfig instead).
# git_update_index_timeout and git_restore_timeout were restored because they ARE wired
# in refresh_scheduler.py _create_snapshot() — operators who tune these in config.json
# must not have their values silently discarded.
_DEAD_RESOURCE_FIELDS = ("cidx_scip_generate_timeout",)

# Genuinely-wired fields that must remain — includes the two restored fields
_LIVE_RESOURCE_FIELDS = (
    "git_init_conflict_timeout",
    "git_service_conflict_timeout",
    "git_service_cleanup_timeout",
    "git_service_wait_timeout",
    "git_process_check_timeout",
    "git_untracked_file_timeout",
    "cow_clone_timeout",
    "cidx_fix_config_timeout",
    # Restored: these ARE wired in refresh_scheduler._create_snapshot()
    "git_update_index_timeout",
    "git_restore_timeout",
)


class TestAC7DeadFieldsRemoved:
    """AC7.1: Only cidx_scip_generate_timeout is dead; the two git timeouts are restored."""

    def test_git_update_index_timeout_present(self, default_config):
        """ServerResourceConfig MUST have git_update_index_timeout (wired in refresh_scheduler)."""
        assert hasattr(default_config.resource_config, "git_update_index_timeout")
        assert default_config.resource_config.git_update_index_timeout == 300

    def test_git_restore_timeout_present(self, default_config):
        """ServerResourceConfig MUST have git_restore_timeout (wired in refresh_scheduler)."""
        assert hasattr(default_config.resource_config, "git_restore_timeout")
        assert default_config.resource_config.git_restore_timeout == 300

    def test_cidx_scip_generate_timeout_absent(self, default_config):
        """ServerResourceConfig must not have cidx_scip_generate_timeout (uses ScipConfig)."""
        assert not hasattr(default_config.resource_config, "cidx_scip_generate_timeout")


class TestAC7LoaderGuards:
    """AC7.1: Loader pops only the truly-dead resource_config field (cidx_scip_generate_timeout)."""

    def test_strips_cidx_scip_generate_timeout(self, tmp_path):
        """Loader must strip cidx_scip_generate_timeout injected into resource_config."""
        loaded = _mutate_and_reload(
            tmp_path,
            lambda raw: raw["resource_config"].update(
                {"cidx_scip_generate_timeout": 999}
            ),
        )
        assert loaded is not None
        assert not hasattr(loaded.resource_config, "cidx_scip_generate_timeout")

    def test_git_update_index_timeout_accepted_from_config(self, tmp_path):
        """Loader must accept git_update_index_timeout from config (it is a real field)."""
        loaded = _mutate_and_reload(
            tmp_path,
            lambda raw: raw["resource_config"].update(
                {"git_update_index_timeout": 1800}
            ),
        )
        assert loaded is not None
        assert hasattr(loaded.resource_config, "git_update_index_timeout")
        assert loaded.resource_config.git_update_index_timeout == 1800

    def test_git_restore_timeout_accepted_from_config(self, tmp_path):
        """Loader must accept git_restore_timeout from config (it is a real field)."""
        loaded = _mutate_and_reload(
            tmp_path,
            lambda raw: raw["resource_config"].update({"git_restore_timeout": 600}),
        )
        assert loaded is not None
        assert hasattr(loaded.resource_config, "git_restore_timeout")
        assert loaded.resource_config.git_restore_timeout == 600

    def test_live_fields_retained_after_strip(self, tmp_path):
        """All 10 genuinely-wired resource_config fields must survive the load."""
        loaded = _mutate_and_reload(
            tmp_path,
            lambda raw: raw["resource_config"].update(
                {"cidx_scip_generate_timeout": 999}
            ),
        )
        for field in _LIVE_RESOURCE_FIELDS:
            assert hasattr(loaded.resource_config, field)
