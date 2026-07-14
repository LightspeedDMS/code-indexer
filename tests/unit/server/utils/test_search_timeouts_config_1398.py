"""Tests for SearchTimeoutsConfig (Issue #1398).

Issue #1398: at least 15 independent hardcoded timeout constants existed
across the MCP tool-call layer, temporal/embedding/reranking pipeline, and
the CLI's remote HTTP client -- none of them Web-UI-configurable. This
dataclass consolidates 5 of them (Groups A/B from the issue) into a single,
validated, Web-UI-editable config section, following the exact
SearchLimitsConfig / HNSWOrphanRepairSweepConfig pattern already established
in this file:

  - search_code_handler_timeout_seconds (default 180) -- replaces
    protocol.py's SEARCH_HANDLER_TIMEOUT_SECONDS (search_code override).
  - default_handler_timeout_seconds (default 60) -- replaces protocol.py's
    HANDLER_TIMEOUT_SECONDS (sync-dispatched tools with no override).
  - write_mode_handler_timeout_seconds (default 720) -- replaces
    protocol.py's WRITE_MODE_HANDLER_TIMEOUT_SECONDS (exit_write_mode).
  - embedding_provider_timeout_seconds (default 30) -- replaces the
    VoyageAIConfig.timeout / CohereConfig.timeout hardcoded defaults at the
    server-side query-embedding construction sites.
  - reranker_timeout_seconds (default 15) -- replaces both hardcoded
    `timeout: float = 15.0` defaults in reranker_clients.py.
"""

from code_indexer.server.utils.config_manager import (
    SearchTimeoutsConfig,
    ServerConfig,
)


class TestSearchTimeoutsConfigDefaults:
    def test_default_search_code_handler_timeout_seconds(self) -> None:
        assert SearchTimeoutsConfig().search_code_handler_timeout_seconds == 180

    def test_default_default_handler_timeout_seconds(self) -> None:
        assert SearchTimeoutsConfig().default_handler_timeout_seconds == 60

    def test_default_write_mode_handler_timeout_seconds(self) -> None:
        assert SearchTimeoutsConfig().write_mode_handler_timeout_seconds == 720

    def test_default_embedding_provider_timeout_seconds(self) -> None:
        assert SearchTimeoutsConfig().embedding_provider_timeout_seconds == 30

    def test_default_reranker_timeout_seconds(self) -> None:
        assert SearchTimeoutsConfig().reranker_timeout_seconds == 15

    def test_fields_are_overridable(self) -> None:
        cfg = SearchTimeoutsConfig(
            search_code_handler_timeout_seconds=200,
            default_handler_timeout_seconds=90,
            write_mode_handler_timeout_seconds=800,
            embedding_provider_timeout_seconds=45,
            reranker_timeout_seconds=20,
        )
        assert cfg.search_code_handler_timeout_seconds == 200
        assert cfg.default_handler_timeout_seconds == 90
        assert cfg.write_mode_handler_timeout_seconds == 800
        assert cfg.embedding_provider_timeout_seconds == 45
        assert cfg.reranker_timeout_seconds == 20


class TestServerConfigWiresSearchTimeoutsConfig:
    def test_server_config_auto_initializes_when_none(self, tmp_path) -> None:
        config = ServerConfig(server_dir=str(tmp_path))
        assert config.search_timeouts_config is not None
        assert isinstance(config.search_timeouts_config, SearchTimeoutsConfig)

    def test_server_config_preserves_explicit_value(self, tmp_path) -> None:
        custom = SearchTimeoutsConfig(search_code_handler_timeout_seconds=42)
        config = ServerConfig(
            server_dir=str(tmp_path),
            search_timeouts_config=custom,
        )
        assert config.search_timeouts_config.search_code_handler_timeout_seconds == 42


class TestDictToServerConfigDeserializesSearchTimeouts:
    """The runtime DB round trip (PG JSONB / SQLite JSON column) flattens
    every nested dataclass to a plain dict via dataclasses.asdict(). Without
    an explicit conversion block in _dict_to_server_config, the field would
    survive as a raw dict and cfg.search_code_handler_timeout_seconds would
    raise AttributeError on every real cluster/solo deployment -- the exact
    Bug #1368 failure mode this file's HNSW sibling test documents."""

    def test_dict_to_server_config_deserializes_search_timeouts_config(
        self, tmp_path
    ) -> None:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "search_timeouts_config": {
                "search_code_handler_timeout_seconds": 210,
                "default_handler_timeout_seconds": 75,
                "write_mode_handler_timeout_seconds": 750,
                "embedding_provider_timeout_seconds": 40,
                "reranker_timeout_seconds": 25,
            },
        }
        config = manager._dict_to_server_config(config_dict)

        assert isinstance(config.search_timeouts_config, SearchTimeoutsConfig), (
            "search_timeouts_config must be a real SearchTimeoutsConfig instance, "
            "not a plain dict (mirrors Bug #1368's fix pattern)"
        )
        assert config.search_timeouts_config.search_code_handler_timeout_seconds == 210
        assert config.search_timeouts_config.default_handler_timeout_seconds == 75
        assert config.search_timeouts_config.write_mode_handler_timeout_seconds == 750
        assert config.search_timeouts_config.embedding_provider_timeout_seconds == 40
        assert config.search_timeouts_config.reranker_timeout_seconds == 25

    def test_dict_to_server_config_filters_unknown_keys_for_rolling_upgrade(
        self, tmp_path
    ) -> None:
        """Unknown keys (e.g. a field removed in a future version) must be
        filtered rather than raising a TypeError -- same fields()-filtered
        pattern as hnsw_orphan_repair_sweep_config."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "search_timeouts_config": {
                "search_code_handler_timeout_seconds": 210,
                "some_future_field_not_yet_known": "ignored",
            },
        }
        config = manager._dict_to_server_config(config_dict)
        assert config.search_timeouts_config.search_code_handler_timeout_seconds == 210


class TestValidateConfigEnforcesSearchTimeoutsRanges:
    """Mirrors search_limits_config's validate_config min/max range pattern."""

    def _manager(self, tmp_path):
        from code_indexer.server.utils.config_manager import ServerConfigManager

        return ServerConfigManager(str(tmp_path))

    def _base_config(self, tmp_path) -> ServerConfig:
        return ServerConfig(server_dir=str(tmp_path))

    def test_valid_defaults_pass_validation(self, tmp_path) -> None:
        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        manager.validate_config(config)  # must not raise

    def test_search_code_handler_timeout_too_low_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.search_timeouts_config.search_code_handler_timeout_seconds = 0
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_search_code_handler_timeout_too_high_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.search_timeouts_config.search_code_handler_timeout_seconds = 100000
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_default_handler_timeout_out_of_range_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.search_timeouts_config.default_handler_timeout_seconds = 0
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_write_mode_handler_timeout_out_of_range_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.search_timeouts_config.write_mode_handler_timeout_seconds = 5
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_embedding_provider_timeout_out_of_range_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.search_timeouts_config.embedding_provider_timeout_seconds = 0
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_reranker_timeout_out_of_range_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.search_timeouts_config.reranker_timeout_seconds = 0
        with pytest.raises(ValueError):
            manager.validate_config(config)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
