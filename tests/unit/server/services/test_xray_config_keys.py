"""
Unit tests for Story #977: X-Ray Config Keys (Web UI Config Screen Integration).

Since modular plan phase 4c the keys are the cidx-xray feature's config
section (``cidx_xray.config``): runtime values in
``ServerConfig.feature_settings["xray"]``, shown and changed only through the
feature config door (the config screen's Feature Settings block and the
config API; ``features/config.commit_runtime_update``).

Acceptance Criteria covered:
  AC1 - Defaults: xray_timeout_seconds=120, xray_worker_threads=4
  AC2 - Admin saves new values; they persist via get_config_service().get_config()
  AC3 - timeout out of range rejected (10..600)
  AC4 - worker_threads out of range rejected (1..8)
  AC7 - Settings are NOT exposed via os.environ
  AC8 - Settings are NOT in bootstrap config.json (they are runtime/DB-backed)
"""

import os
from typing import Any

import pytest

from code_indexer.server.features import config as feature_config


# ---------------------------------------------------------------------------
# The feature's section: defaults and kinds
# ---------------------------------------------------------------------------


class TestXRaySectionDefaults:
    """The feature section's keys and default values."""

    def test_defaults(self):
        from cidx_xray.config import default_values

        values = default_values()
        assert values["xray_timeout_seconds"] == 120
        assert values["xray_worker_threads"] == 4

    def test_timeout_and_worker_threads_are_runtime_keys(self):
        from cidx_xray.config import XRAY_CONFIG_SECTION

        assert XRAY_CONFIG_SECTION.runtime_keys == {
            "xray_timeout_seconds",
            "xray_worker_threads",
        }
        assert XRAY_CONFIG_SECTION.bootstrap_keys == {"xray_backend"}


class TestServerConfigNoLongerOwnsXRay:
    """ServerConfig has no X-Ray fields; stored values move into the section."""

    def test_server_config_has_no_xray_fields(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        fields = ServerConfig.__dataclass_fields__
        assert "xray_config" not in fields
        assert "xray_backend" not in fields

    def test_dict_to_server_config_moves_xray_config(self, tmp_path):
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config = manager._dict_to_server_config(
            {
                "server_dir": str(tmp_path),
                "xray_config": {"xray_timeout_seconds": 30, "xray_worker_threads": 6},
            }
        )
        assert config.feature_settings["xray"] == {
            "xray_timeout_seconds": 30,
            "xray_worker_threads": 6,
        }


# ---------------------------------------------------------------------------
# Persistence through the feature config door
# ---------------------------------------------------------------------------


def _set_xray(service: Any, **updates: Any) -> None:
    from cidx_xray.config import XRAY_CONFIG_SECTION

    feature_config.commit_runtime_update(
        service, "xray", XRAY_CONFIG_SECTION, updates, "admin"
    )


class TestXRayConfigPersistence:
    """The door commits the keys through the config service."""

    def _service(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())
        return ConfigService(str(tmp_path))

    def test_listed_default_values(self, tmp_path):
        from cidx_xray.config import XRAY_CONFIG_SECTION

        (section,) = feature_config.describe_sections(
            [("xray", XRAY_CONFIG_SECTION)], self._service(tmp_path).get_config()
        )
        values = {key["name"]: key["value"] for key in section["keys"]}
        assert values["xray_timeout_seconds"] == 120
        assert values["xray_worker_threads"] == 4

    def test_update_xray_timeout_seconds_persists(self, tmp_path):
        config_service = self._service(tmp_path)
        _set_xray(config_service, xray_timeout_seconds=60)
        stored = config_service.get_config().feature_settings["xray"]
        assert stored["xray_timeout_seconds"] == 60

    def test_save_and_reload_persists_xray_keys(self, tmp_path):
        """Write values, create a new ConfigService, assert they persist after reload."""
        from code_indexer.server.services.config_service import ConfigService

        svc1 = self._service(tmp_path)
        _set_xray(svc1, xray_timeout_seconds=300, xray_worker_threads=6)

        svc2 = ConfigService(str(tmp_path))
        assert svc2.get_config().feature_settings["xray"] == {
            "xray_timeout_seconds": 300,
            "xray_worker_threads": 6,
        }

    def test_update_unknown_xray_key_refused(self, tmp_path):
        with pytest.raises(feature_config.FeatureConfigError):
            _set_xray(self._service(tmp_path), unknown_key=99)

    def test_update_bootstrap_key_refused(self, tmp_path):
        with pytest.raises(feature_config.FeatureConfigError, match="bootstrap"):
            _set_xray(self._service(tmp_path), xray_backend="delegated")

    def test_the_core_has_no_xray_category(self, tmp_path):
        """Only the feature config door changes a feature's settings."""
        service = self._service(tmp_path)
        assert "xray" not in service.get_all_settings()
        with pytest.raises(ValueError, match="Unknown category"):
            service.update_setting("xray", "xray_timeout_seconds", 60)


# ---------------------------------------------------------------------------
# AC3/AC4 - ranges (the section's bounds, enforced by the door)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value,accepted",
    [
        ("xray_timeout_seconds", 5, False),
        ("xray_timeout_seconds", 0, False),
        ("xray_timeout_seconds", 900, False),
        ("xray_timeout_seconds", 10, True),
        ("xray_timeout_seconds", 600, True),
        ("xray_worker_threads", 0, False),
        ("xray_worker_threads", 9, False),
        ("xray_worker_threads", 1, True),
        ("xray_worker_threads", 8, True),
        ("xray_worker_threads", "abc", False),
    ],
)
def test_xray_ranges(key: str, value: Any, accepted: bool) -> None:
    from cidx_xray.config import XRAY_CONFIG_SECTION

    if accepted:
        assert feature_config.validate_runtime_update(
            "xray", XRAY_CONFIG_SECTION, {key: value}
        ) == {key: value}
    else:
        with pytest.raises(feature_config.FeatureConfigError):
            feature_config.validate_runtime_update(
                "xray", XRAY_CONFIG_SECTION, {key: value}
            )


# ---------------------------------------------------------------------------
# AC7 - Settings NOT exposed via os.environ
# ---------------------------------------------------------------------------


class TestXRayNotInEnvironment:
    """X-Ray settings must not be exposed via os.environ."""

    def test_xray_timeout_not_in_environ(self):
        """XRAY_TIMEOUT_SECONDS is not set in os.environ by CIDX."""
        assert "XRAY_TIMEOUT_SECONDS" not in os.environ

    def test_xray_worker_threads_not_in_environ(self):
        """XRAY_WORKER_THREADS is not set in os.environ by CIDX."""
        assert "XRAY_WORKER_THREADS" not in os.environ


# ---------------------------------------------------------------------------
# AC8 - Settings NOT in BOOTSTRAP_KEYS
# ---------------------------------------------------------------------------


class TestXRayNotInBootstrapKeys:
    """X-Ray config keys must be runtime-only, NOT bootstrap."""

    def test_xray_timeout_not_in_bootstrap_keys(self):
        """xray_timeout_seconds is not in BOOTSTRAP_KEYS (it is runtime)."""
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "xray_timeout_seconds" not in BOOTSTRAP_KEYS

    def test_xray_worker_threads_not_in_bootstrap_keys(self):
        """xray_worker_threads is not in BOOTSTRAP_KEYS (it is runtime)."""
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "xray_worker_threads" not in BOOTSTRAP_KEYS

    def test_xray_config_not_in_bootstrap_keys(self):
        """xray_config is not in BOOTSTRAP_KEYS (it is runtime)."""
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        assert "xray_config" not in BOOTSTRAP_KEYS
