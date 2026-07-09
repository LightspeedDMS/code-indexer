"""Story #1079 Phase E — coalescer runtime config field tests.

The embedding coalescer adds four RUNTIME (DB-backed, Web-UI tunable, NOT
bootstrap) top-level ServerConfig fields:

  - coalesce_enabled (bool, default True)        — kill switch
  - coalesce_max_batch_size (int, default 96)    — hard ceiling (== Cohere cap)
  - coalesce_k_min (int, default 8)              — AIMD floor seed (K_MIN)
  - coalesce_k_max (int, default 32)             — AIMD ceiling seed (K_MAX)

They mirror query_provider_max_concurrency exactly: plain top-level fields read
live via getattr, auto-persisted through asdict() minus BOOTSTRAP_KEYS, and
deserialized via _dict_to_server_config. Rolling-restart safe: old blobs that
omit these keys must deserialize to the defaults.
"""

import pytest

from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager

DEFAULT_MAX_BATCH_SIZE = 96
DEFAULT_K_MIN = 8
DEFAULT_K_MAX = 32


@pytest.fixture
def manager(tmp_path):
    return ServerConfigManager(str(tmp_path))


class TestCoalesceFieldDefaults:
    def test_server_config_defaults(self, tmp_path):
        cfg = ServerConfig(server_dir=str(tmp_path))
        assert cfg.coalesce_enabled is True
        assert cfg.coalesce_max_batch_size == DEFAULT_MAX_BATCH_SIZE
        assert cfg.coalesce_k_min == DEFAULT_K_MIN
        assert cfg.coalesce_k_max == DEFAULT_K_MAX

    def test_fields_are_not_bootstrap_keys(self):
        """Coalescer fields are RUNTIME (DB-backed), never bootstrap config.json."""
        from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

        for field in (
            "coalesce_enabled",
            "coalesce_max_batch_size",
            "coalesce_k_min",
            "coalesce_k_max",
        ):
            assert field not in BOOTSTRAP_KEYS, (
                f"{field} must be a runtime field, not a bootstrap key"
            )


class TestCoalesceRoundTrip:
    def test_explicit_values_round_trip(self, manager, tmp_path):
        cfg = manager._dict_to_server_config(
            {
                "server_dir": str(tmp_path),
                "coalesce_enabled": False,
                "coalesce_max_batch_size": 48,
                "coalesce_k_min": 4,
                "coalesce_k_max": 64,
            }
        )
        assert cfg.coalesce_enabled is False
        assert cfg.coalesce_max_batch_size == 48
        assert cfg.coalesce_k_min == 4
        assert cfg.coalesce_k_max == 64


class TestCoalesceBackwardCompat:
    def test_old_blob_missing_fields_uses_defaults(self, manager, tmp_path):
        """An old runtime blob without the new keys deserializes to defaults."""
        cfg = manager._dict_to_server_config({"server_dir": str(tmp_path)})
        assert cfg.coalesce_enabled is True
        assert cfg.coalesce_max_batch_size == DEFAULT_MAX_BATCH_SIZE
        assert cfg.coalesce_k_min == DEFAULT_K_MIN
        assert cfg.coalesce_k_max == DEFAULT_K_MAX


class TestCoalesceConfigUpdate:
    """The 4 coalesce keys are tunable via the existing config-update path.

    This mirrors how server runtime settings are exposed (the same generic
    server-settings update mechanism the Web UI / config API drives). The kill
    switch (coalesce_enabled=False) flows through here.
    """

    def _service(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        return ConfigService(server_dir_path=str(tmp_path))

    def test_update_coalesce_settings(self, tmp_path):
        svc = self._service(tmp_path)
        cfg = svc.get_config()

        svc._update_server_setting(cfg, "coalesce_enabled", False)
        assert cfg.coalesce_enabled is False

        svc._update_server_setting(cfg, "coalesce_max_batch_size", 64)
        assert cfg.coalesce_max_batch_size == 64

        svc._update_server_setting(cfg, "coalesce_k_min", 4)
        assert cfg.coalesce_k_min == 4

        svc._update_server_setting(cfg, "coalesce_k_max", 24)
        assert cfg.coalesce_k_max == 24
