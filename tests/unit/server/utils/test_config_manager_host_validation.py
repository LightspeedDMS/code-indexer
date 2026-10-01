"""
ServerConfigManager.validate_config must reject an invalid `host` value
and an out-of-range `workers` value.

validate_config bounds-checks port, jwt_expiration_minutes, log_level,
host, and workers, matching the existing explicit range checks for
mcp_dispatch_pool_size / query_executor_pool_size a bit further down in
the same method. This makes the atomic validate-then-publish flow
(update_settings_atomic -> validate_config) itself refuse a bad host or an
out-of-range workers count even if some future call site bypasses
ConfigService._update_server_setting.
"""

import pytest

from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager

_MULTILINE_HOST = "example.com\nExecStartPre=+/bin/true"


def _manager(tmp_path) -> ServerConfigManager:
    return ServerConfigManager(server_dir_path=tmp_path)


class TestValidateConfigRejectsInvalidHost:
    def test_multiline_host_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), host=_MULTILINE_HOST)
        with pytest.raises(ValueError):
            _manager(tmp_path).validate_config(config)

    def test_host_with_space_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), host="example.com other")
        with pytest.raises(ValueError):
            _manager(tmp_path).validate_config(config)

    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "127.0.0.1", "localhost", "example.com", "::"]
    )
    def test_legitimate_host_is_accepted(self, tmp_path, host):
        config = ServerConfig(server_dir=str(tmp_path), host=host)
        _manager(tmp_path).validate_config(config)  # must not raise


class TestValidateConfigOnlyValidatesAChangedHost:
    """With previous_host (the persisted value), an unchanged host is not
    re-validated; a changed one always is. Without it, host is always
    validated."""

    _LEGACY = "fe80::1%eth0"

    def test_unchanged_legacy_host_is_not_revalidated(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), host=self._LEGACY)
        _manager(tmp_path).validate_config(config, previous_host=self._LEGACY)

    def test_changed_invalid_host_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), host=_MULTILINE_HOST)
        with pytest.raises(ValueError):
            _manager(tmp_path).validate_config(config, previous_host="0.0.0.0")

    def test_legacy_host_without_previous_host_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), host=self._LEGACY)
        with pytest.raises(ValueError):
            _manager(tmp_path).validate_config(config)


class TestValidateConfigRejectsOutOfRangeWorkers:
    def test_workers_zero_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), workers=0)
        with pytest.raises(ValueError, match="workers"):
            _manager(tmp_path).validate_config(config)

    def test_workers_negative_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), workers=-1)
        with pytest.raises(ValueError, match="workers"):
            _manager(tmp_path).validate_config(config)

    def test_workers_above_max_is_rejected(self, tmp_path):
        config = ServerConfig(server_dir=str(tmp_path), workers=65)
        with pytest.raises(ValueError, match="workers"):
            _manager(tmp_path).validate_config(config)

    @pytest.mark.parametrize("workers", [1, 2, 64])
    def test_workers_in_range_is_accepted(self, tmp_path, workers):
        config = ServerConfig(server_dir=str(tmp_path), workers=workers)
        _manager(tmp_path).validate_config(config)  # must not raise
