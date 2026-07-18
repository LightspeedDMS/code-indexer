"""Tests for EmbeddingStatsConfig (Story #1418 Phase 3 of 3).

Consolidates the embedding/reranker call-tracking tunables (on/off
kill-switch, writer flush cadence, retention window) into a single,
validated, Web-UI-editable config section, following the exact
SearchTimeoutsConfig / HNSWOrphanRepairSweepConfig pattern already
established in this file.

  - enabled (default True) -- global kill-switch; when False,
    EmbeddingStatsWriter.get_active() resolves to NoOpWriter regardless of
    what writer was previously installed.
  - flush_interval_seconds (default 30.0) -- MUST match
    embedding_stats_writer.py's pre-Phase-3 hardcoded
    _DEFAULT_FLUSH_INTERVAL_SECONDS so turning on config-driven tuning does
    not silently change existing behavior.
  - retention_days (default 90) -- cutoff window for the retention sweep's
    backend.delete_where(occurred_at_before=...) call.
"""

from code_indexer.server.utils.config_manager import (
    EmbeddingStatsConfig,
    ServerConfig,
)


class TestEmbeddingStatsConfigDefaults:
    def test_default_enabled(self) -> None:
        assert EmbeddingStatsConfig().enabled is True

    def test_default_flush_interval_seconds_matches_pre_phase3_hardcoded_default(
        self,
    ) -> None:
        assert EmbeddingStatsConfig().flush_interval_seconds == 30.0

    def test_default_retention_days(self) -> None:
        assert EmbeddingStatsConfig().retention_days == 90

    def test_fields_are_overridable(self) -> None:
        cfg = EmbeddingStatsConfig(
            enabled=False, flush_interval_seconds=5.0, retention_days=30
        )
        assert cfg.enabled is False
        assert cfg.flush_interval_seconds == 5.0
        assert cfg.retention_days == 30


class TestServerConfigWiresEmbeddingStatsConfig:
    def test_server_config_auto_initializes_when_none(self, tmp_path) -> None:
        config = ServerConfig(server_dir=str(tmp_path))
        assert config.embedding_stats_config is not None
        assert isinstance(config.embedding_stats_config, EmbeddingStatsConfig)

    def test_server_config_preserves_explicit_value(self, tmp_path) -> None:
        custom = EmbeddingStatsConfig(retention_days=7)
        config = ServerConfig(
            server_dir=str(tmp_path),
            embedding_stats_config=custom,
        )
        assert config.embedding_stats_config.retention_days == 7


class TestDictToServerConfigDeserializesEmbeddingStats:
    """The runtime DB round trip (PG JSONB / SQLite JSON column) flattens
    every nested dataclass to a plain dict via dataclasses.asdict(). Without
    an explicit conversion block in _dict_to_server_config, the field would
    survive as a raw dict and cfg.retention_days would raise AttributeError
    on every real cluster/solo deployment."""

    def test_dict_to_server_config_deserializes_embedding_stats_config(
        self, tmp_path
    ) -> None:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "embedding_stats_config": {
                "enabled": False,
                "flush_interval_seconds": 10.0,
                "retention_days": 15,
            },
        }
        config = manager._dict_to_server_config(config_dict)

        assert isinstance(config.embedding_stats_config, EmbeddingStatsConfig), (
            "embedding_stats_config must be a real EmbeddingStatsConfig "
            "instance, not a plain dict"
        )
        assert config.embedding_stats_config.enabled is False
        assert config.embedding_stats_config.flush_interval_seconds == 10.0
        assert config.embedding_stats_config.retention_days == 15

    def test_dict_to_server_config_filters_unknown_keys_for_rolling_upgrade(
        self, tmp_path
    ) -> None:
        """Unknown keys (e.g. a field removed in a future version) must be
        filtered rather than raising a TypeError."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "embedding_stats_config": {
                "retention_days": 45,
                "some_future_field_not_yet_known": "ignored",
            },
        }
        config = manager._dict_to_server_config(config_dict)
        assert config.embedding_stats_config.retention_days == 45


class TestValidateConfigEnforcesEmbeddingStatsRanges:
    def _manager(self, tmp_path):
        from code_indexer.server.utils.config_manager import ServerConfigManager

        return ServerConfigManager(str(tmp_path))

    def _base_config(self, tmp_path) -> ServerConfig:
        return ServerConfig(server_dir=str(tmp_path))

    def test_valid_defaults_pass_validation(self, tmp_path) -> None:
        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        manager.validate_config(config)  # must not raise

    def test_flush_interval_seconds_zero_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.embedding_stats_config.flush_interval_seconds = 0
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_flush_interval_seconds_negative_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.embedding_stats_config.flush_interval_seconds = -1.0
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_retention_days_zero_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.embedding_stats_config.retention_days = 0
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_retention_days_negative_rejected(self, tmp_path) -> None:
        import pytest

        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.embedding_stats_config.retention_days = -5
        with pytest.raises(ValueError):
            manager.validate_config(config)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
