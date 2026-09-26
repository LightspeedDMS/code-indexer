"""
ConfigService must reject an invalid `host` value before it is persisted.

_update_server_setting must validate the host (an IP literal or an RFC
1123 hostname) before assigning it. update_settings_atomic validates a
deep-copied CANDIDATE via config_manager.validate_config() and only calls
save_config() on success -- a ValueError raised while applying/validating
the candidate means the update never persists and the live config is left
untouched.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import ServerConfig

_MULTILINE_HOST = "example.com\nExecStartPre=+/bin/true"


def _make_service(tmp_path) -> ConfigService:
    return ConfigService(server_dir_path=str(tmp_path))


class TestUpdateServerSettingRejectsInvalidHost:
    """Direct unit-level coverage of _update_server_setting itself."""

    def test_multiline_host_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path))
        svc = _make_service(tmp_path)
        with pytest.raises(ValueError):
            svc._update_server_setting(config, "host", _MULTILINE_HOST)
        # Must not have mutated the config on the way to raising.
        assert config.host != _MULTILINE_HOST

    def test_legitimate_host_still_accepted(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path))
        svc = _make_service(tmp_path)
        svc._update_server_setting(config, "host", "192.0.2.10")
        assert config.host == "192.0.2.10"


class TestUpdateSettingsAtomicRejectsInvalidHost:
    """Full atomic validate-copy-then-publish path (update_settings_atomic)."""

    def test_multiline_host_raises_and_nothing_persists(self, tmp_path):
        svc = _make_service(tmp_path)
        original_host = svc.get_config().host

        with pytest.raises(ValueError):
            svc.update_setting("server", "host", _MULTILINE_HOST)

        # Live in-memory config must be unchanged (CRITICAL 6 atomicity).
        assert svc.get_config().host == original_host

        # A freshly-loaded service (re-reads persisted state) must also be
        # unchanged -- proves the invalid value never reached storage.
        svc2 = _make_service(tmp_path)
        assert svc2.get_config().host == original_host
        assert svc2.get_config().host != _MULTILINE_HOST

    def test_legitimate_host_change_persists(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.update_setting("server", "host", "192.0.2.10")
        assert svc.get_config().host == "192.0.2.10"

        svc2 = _make_service(tmp_path)
        assert svc2.get_config().host == "192.0.2.10"
