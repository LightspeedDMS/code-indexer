"""
Unit tests for Story #977: X-Ray Config Keys (Web UI Config Screen Integration).

TDD: Tests written BEFORE implementation (Red phase). All should fail until
XRayConfig is added to config_manager.py and ConfigService/routes are updated.

Acceptance Criteria covered:
  AC1 - Defaults: xray_timeout_seconds=120, xray_worker_threads=2
  AC2 - Admin saves new values; they persist via get_config_service().get_config()
  AC3 - timeout out of range rejected (10..600)
  AC4 - worker_threads out of range rejected (1..8)
  AC7 - Settings are NOT exposed via os.environ
  AC8 - Settings are NOT in bootstrap config.json (they are runtime/DB-backed)
"""

import os
import pytest


# ---------------------------------------------------------------------------
# XRayConfig dataclass
# ---------------------------------------------------------------------------


class TestXRayConfigDefaults:
    """XRayConfig must have correct default values."""

    def test_xray_timeout_seconds_default_120(self):
        """XRayConfig default xray_timeout_seconds is 120."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig()
        assert cfg.xray_timeout_seconds == 120

    def test_xray_worker_threads_default_2(self):
        """XRayConfig default xray_worker_threads is 2."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig()
        assert cfg.xray_worker_threads == 2

    def test_can_override_defaults(self):
        """XRayConfig accepts custom values."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig(xray_timeout_seconds=60, xray_worker_threads=4)
        assert cfg.xray_timeout_seconds == 60
        assert cfg.xray_worker_threads == 4


# ---------------------------------------------------------------------------
# Range validation at the dataclass level (accepts in-range values)
# ---------------------------------------------------------------------------


class TestXRayConfigAcceptsValidRanges:
    """XRayConfig accepts all in-range values without error."""

    def test_xray_timeout_seconds_accepts_min_10(self):
        """xray_timeout_seconds=10 is valid (minimum boundary)."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig(xray_timeout_seconds=10)
        assert cfg.xray_timeout_seconds == 10

    def test_xray_timeout_seconds_accepts_120(self):
        """xray_timeout_seconds=120 is valid (default)."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig(xray_timeout_seconds=120)
        assert cfg.xray_timeout_seconds == 120

    def test_xray_timeout_seconds_accepts_max_600(self):
        """xray_timeout_seconds=600 is valid (maximum boundary)."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig(xray_timeout_seconds=600)
        assert cfg.xray_timeout_seconds == 600

    def test_xray_worker_threads_accepts_min_1(self):
        """xray_worker_threads=1 is valid (minimum boundary)."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig(xray_worker_threads=1)
        assert cfg.xray_worker_threads == 1

    def test_xray_worker_threads_accepts_4(self):
        """xray_worker_threads=4 is valid (midrange)."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig(xray_worker_threads=4)
        assert cfg.xray_worker_threads == 4

    def test_xray_worker_threads_accepts_max_8(self):
        """xray_worker_threads=8 is valid (maximum boundary)."""
        from code_indexer.server.utils.config_manager import XRayConfig

        cfg = XRayConfig(xray_worker_threads=8)
        assert cfg.xray_worker_threads == 8


# ---------------------------------------------------------------------------
# ServerConfig field presence
# ---------------------------------------------------------------------------


class TestServerConfigXRayField:
    """ServerConfig must declare xray_config field."""

    def test_server_config_has_xray_config_field(self):
        """ServerConfig dataclass must have an xray_config field."""
        from code_indexer.server.utils.config_manager import ServerConfig

        assert hasattr(ServerConfig, "__dataclass_fields__")
        assert "xray_config" in ServerConfig.__dataclass_fields__

    def test_post_init_initializes_xray_config(self, tmp_path):
        """ServerConfig.__post_init__ sets xray_config to default when None."""
        from code_indexer.server.utils.config_manager import (
            XRayConfig,
            ServerConfigManager,
        )

        manager = ServerConfigManager(str(tmp_path))
        config = manager.create_default_config()

        assert config.xray_config is not None
        assert isinstance(config.xray_config, XRayConfig)
        assert config.xray_config.xray_timeout_seconds == 120
        assert config.xray_config.xray_worker_threads == 2

    def test_dict_to_server_config_deserializes_xray(self, tmp_path):
        """_dict_to_server_config converts xray_config dict to XRayConfig dataclass."""
        from code_indexer.server.utils.config_manager import (
            XRayConfig,
            ServerConfigManager,
        )

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "xray_config": {"xray_timeout_seconds": 30, "xray_worker_threads": 6},
        }
        config = manager._dict_to_server_config(config_dict)

        assert isinstance(config.xray_config, XRayConfig)
        assert config.xray_config.xray_timeout_seconds == 30
        assert config.xray_config.xray_worker_threads == 6


# ---------------------------------------------------------------------------
# ConfigService persistence
# ---------------------------------------------------------------------------


class TestXRayConfigServicePersistence:
    """ConfigService.get_all_settings() and update_setting() for xray."""

    def test_get_settings_includes_xray_section(self, tmp_path):
        """ConfigService.get_all_settings() includes xray section."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        settings = config_service.get_all_settings()

        assert "xray" in settings

    def test_get_settings_xray_has_required_keys(self, tmp_path):
        """xray section must have xray_timeout_seconds and xray_worker_threads keys."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        xray = config_service.get_all_settings()["xray"]

        assert "xray_timeout_seconds" in xray
        assert "xray_worker_threads" in xray

    def test_get_settings_xray_default_values(self, tmp_path):
        """xray section returns correct default values."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        xray = config_service.get_all_settings()["xray"]

        assert xray["xray_timeout_seconds"] == 120
        assert xray["xray_worker_threads"] == 2

    def test_update_xray_timeout_seconds_persists(self, tmp_path):
        """update_setting('xray', 'xray_timeout_seconds', 60) persists to config."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting("xray", "xray_timeout_seconds", 60)

        assert config_service.get_config().xray_config.xray_timeout_seconds == 60

    def test_update_xray_worker_threads_persists(self, tmp_path):
        """update_setting('xray', 'xray_worker_threads', 4) persists to config."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))
        config_service.update_setting("xray", "xray_worker_threads", 4)

        assert config_service.get_config().xray_config.xray_worker_threads == 4

    def test_save_and_reload_persists_xray_keys(self, tmp_path):
        """Write values, create a new ConfigService, assert they persist after reload."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        svc1 = ConfigService(str(tmp_path))
        svc1.update_setting("xray", "xray_timeout_seconds", 300)
        svc1.update_setting("xray", "xray_worker_threads", 6)

        # Simulate restart: new service instance loads from disk
        svc2 = ConfigService(str(tmp_path))
        config = svc2.get_config()
        assert config.xray_config.xray_timeout_seconds == 300
        assert config.xray_config.xray_worker_threads == 6

    def test_update_unknown_xray_key_raises_value_error(self, tmp_path):
        """update_setting('xray', 'unknown_key', 99) raises ValueError."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(str(tmp_path))
        mgr.save_config(mgr.create_default_config())

        config_service = ConfigService(str(tmp_path))

        with pytest.raises(ValueError):
            config_service.update_setting("xray", "unknown_key", 99)


# ---------------------------------------------------------------------------
# Route validation (matches _validate_config_section in routes.py)
# ---------------------------------------------------------------------------


class TestXRayRouteValidation:
    """Route-level validation for xray section must enforce ranges."""

    def _validate(self, data: dict):
        """Call the routes._validate_config_section helper directly."""
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("xray", data)

    def test_xray_timeout_seconds_rejects_below_10(self):
        """timeout_seconds=5 is below range, returns error message."""
        error = self._validate({"xray_timeout_seconds": "5"})
        assert error is not None
        assert "10" in error or "timeout" in error.lower()

    def test_xray_timeout_seconds_rejects_above_600(self):
        """timeout_seconds=900 is above range, returns error message."""
        error = self._validate({"xray_timeout_seconds": "900"})
        assert error is not None
        assert "600" in error or "timeout" in error.lower()

    def test_xray_timeout_seconds_rejects_zero(self):
        """timeout_seconds=0 is below range, returns error message."""
        error = self._validate({"xray_timeout_seconds": "0"})
        assert error is not None

    def test_xray_timeout_seconds_accepts_boundary_10(self):
        """timeout_seconds=10 at minimum boundary is valid (no error)."""
        error = self._validate({"xray_timeout_seconds": "10"})
        assert error is None

    def test_xray_timeout_seconds_accepts_boundary_600(self):
        """timeout_seconds=600 at maximum boundary is valid (no error)."""
        error = self._validate({"xray_timeout_seconds": "600"})
        assert error is None

    def test_xray_worker_threads_rejects_below_1(self):
        """worker_threads=0 is below range, returns error message."""
        error = self._validate({"xray_worker_threads": "0"})
        assert error is not None
        assert "1" in error or "thread" in error.lower() or "worker" in error.lower()

    def test_xray_worker_threads_rejects_above_8(self):
        """worker_threads=9 is above range, returns error message."""
        error = self._validate({"xray_worker_threads": "9"})
        assert error is not None
        assert "8" in error or "thread" in error.lower() or "worker" in error.lower()

    def test_xray_worker_threads_accepts_boundary_1(self):
        """worker_threads=1 at minimum boundary is valid (no error)."""
        error = self._validate({"xray_worker_threads": "1"})
        assert error is None

    def test_xray_worker_threads_accepts_boundary_8(self):
        """worker_threads=8 at maximum boundary is valid (no error)."""
        error = self._validate({"xray_worker_threads": "8"})
        assert error is None

    def test_xray_valid_section_returns_none(self):
        """Valid data for xray section returns None (no error)."""
        error = self._validate(
            {"xray_timeout_seconds": "120", "xray_worker_threads": "2"}
        )
        assert error is None

    def test_xray_timeout_seconds_rejects_non_numeric(self):
        """Non-numeric timeout_seconds value returns error."""
        error = self._validate({"xray_timeout_seconds": "abc"})
        assert error is not None

    def test_xray_worker_threads_rejects_non_numeric(self):
        """Non-numeric worker_threads value returns error."""
        error = self._validate({"xray_worker_threads": "abc"})
        assert error is not None


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
