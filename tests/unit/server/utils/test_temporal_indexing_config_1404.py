"""Tests for TemporalIndexingConfig (Story #1404).

Story #1404: a single global, DB-backed "temporal indexing floor date"
bounding ALL FUTURE `cidx index --index-commits` runs across the fleet.
Commits dated on/after the floor are indexed; older commits are skipped
(maps to `git log --since`). Forward-only: bounds future runs only, never
retroactively prunes already-indexed commits. None/empty = unbounded =
byte-identical to today's full-history behavior (safety no-op).

Follows the exact SearchTimeoutsConfig / EmbeddingStatsConfig two-layer
pattern already established in this file. Validation mirrors
temporal_search_service.parse_date_range's shape: strptime for real-
calendar-date rejection (e.g. 2026-02-30) plus a strftime round-trip for
zero-padding rejection (e.g. 2026-1-1).
"""

import pytest

from code_indexer.server.utils.config_manager import (
    TemporalIndexingConfig,
    ServerConfig,
)


class TestTemporalIndexingConfigDefaults:
    def test_default_index_floor_date_is_none(self) -> None:
        assert TemporalIndexingConfig().index_floor_date is None

    def test_field_is_overridable(self) -> None:
        cfg = TemporalIndexingConfig(index_floor_date="2025-01-01")
        assert cfg.index_floor_date == "2025-01-01"


class TestTemporalIndexingConfigValidate:
    def test_none_is_valid_unbounded(self) -> None:
        TemporalIndexingConfig(index_floor_date=None).validate()  # must not raise

    def test_empty_string_is_valid_unbounded(self) -> None:
        TemporalIndexingConfig(index_floor_date="").validate()  # must not raise

    def test_valid_date_passes(self) -> None:
        TemporalIndexingConfig(index_floor_date="2025-01-01").validate()  # no raise

    def test_non_real_calendar_date_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalIndexingConfig(index_floor_date="2026-02-30").validate()

    def test_non_zero_padded_date_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalIndexingConfig(index_floor_date="2026-1-1").validate()

    def test_garbage_string_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalIndexingConfig(index_floor_date="not-a-date").validate()

    def test_wrong_separator_rejected(self) -> None:
        with pytest.raises(ValueError):
            TemporalIndexingConfig(index_floor_date="2025/01/01").validate()


class TestServerConfigWiresTemporalIndexingConfig:
    def test_server_config_auto_initializes_when_none(self, tmp_path) -> None:
        config = ServerConfig(server_dir=str(tmp_path))
        assert config.temporal_indexing_config is not None
        assert isinstance(config.temporal_indexing_config, TemporalIndexingConfig)

    def test_server_config_preserves_explicit_value(self, tmp_path) -> None:
        custom = TemporalIndexingConfig(index_floor_date="2024-06-01")
        config = ServerConfig(
            server_dir=str(tmp_path),
            temporal_indexing_config=custom,
        )
        assert config.temporal_indexing_config.index_floor_date == "2024-06-01"


class TestDictToServerConfigDeserializesTemporalIndexing:
    """The runtime DB round trip (PG JSONB / SQLite JSON column) flattens
    every nested dataclass to a plain dict via dataclasses.asdict(). Without
    an explicit conversion block in _dict_to_server_config, the field would
    survive as a raw dict and cfg.index_floor_date would raise
    AttributeError on every real cluster/solo deployment -- the exact
    Bug #1368 failure mode this file's HNSW/SearchTimeouts sibling tests
    document. Unknown keys (e.g. a field removed in a future version) must
    be filtered rather than raising a TypeError -- same fields()-filtered
    pattern as search_timeouts_config / hnsw_orphan_repair_sweep_config,
    required for rolling-upgrade safety."""

    def test_dict_to_server_config_deserializes_temporal_indexing_config(
        self, tmp_path
    ) -> None:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "temporal_indexing_config": {"index_floor_date": "2025-03-15"},
        }
        config = manager._dict_to_server_config(config_dict)

        assert isinstance(config.temporal_indexing_config, TemporalIndexingConfig), (
            "temporal_indexing_config must be a real TemporalIndexingConfig "
            "instance, not a plain dict"
        )
        assert config.temporal_indexing_config.index_floor_date == "2025-03-15"

    def test_dict_to_server_config_filters_unknown_keys_for_rolling_upgrade(
        self, tmp_path
    ) -> None:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "temporal_indexing_config": {
                "index_floor_date": "2025-03-15",
                "some_future_field_not_yet_known": "ignored",
            },
        }
        config = manager._dict_to_server_config(config_dict)
        assert config.temporal_indexing_config.index_floor_date == "2025-03-15"


class TestValidateConfigEnforcesTemporalIndexingValidation:
    """Mirrors SearchTimeoutsConfig's validate_config integration pattern:
    ServerConfigManager.validate_config() must call
    TemporalIndexingConfig.validate() so a malformed value is rejected at
    the same choke point as every other runtime config section."""

    def _manager(self, tmp_path):
        from code_indexer.server.utils.config_manager import ServerConfigManager

        return ServerConfigManager(str(tmp_path))

    def _base_config(self, tmp_path) -> ServerConfig:
        return ServerConfig(server_dir=str(tmp_path))

    def test_valid_defaults_pass_validation(self, tmp_path) -> None:
        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        manager.validate_config(config)  # must not raise

    def test_valid_floor_date_passes_validation(self, tmp_path) -> None:
        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.temporal_indexing_config.index_floor_date = "2025-01-01"
        manager.validate_config(config)  # must not raise

    def test_malformed_floor_date_rejected(self, tmp_path) -> None:
        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.temporal_indexing_config.index_floor_date = "2026-02-30"
        with pytest.raises(ValueError):
            manager.validate_config(config)

    def test_non_zero_padded_floor_date_rejected(self, tmp_path) -> None:
        manager = self._manager(tmp_path)
        config = self._base_config(tmp_path)
        config.temporal_indexing_config.index_floor_date = "2026-1-1"
        with pytest.raises(ValueError):
            manager.validate_config(config)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
