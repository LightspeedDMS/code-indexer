"""Tests for HNSWOrphanRepairSweepConfig (Story #1360 AC4).

Settled defaults (2026-07-11): ships ON by default with conservative pacing
(~10-20 items/tick, ~5-10 min interval), both operator-adjustable at runtime
via the Web UI config screen (get_config_service(); NO env vars).
"""

from code_indexer.server.utils.config_manager import (
    HNSWOrphanRepairSweepConfig,
    ServerConfig,
)


class TestHNSWOrphanRepairSweepConfigDefaults:
    def test_default_enabled_is_true(self) -> None:
        assert HNSWOrphanRepairSweepConfig().enabled is True

    def test_default_batch_size_in_settled_range(self) -> None:
        cfg = HNSWOrphanRepairSweepConfig()
        assert 10 <= cfg.batch_size <= 20

    def test_default_tick_interval_minutes_in_settled_range(self) -> None:
        cfg = HNSWOrphanRepairSweepConfig()
        assert 5 <= cfg.tick_interval_minutes <= 10

    def test_fields_are_overridable(self) -> None:
        cfg = HNSWOrphanRepairSweepConfig(
            enabled=False, batch_size=42, tick_interval_minutes=3
        )
        assert cfg.enabled is False
        assert cfg.batch_size == 42
        assert cfg.tick_interval_minutes == 3

    def test_default_operating_hours_window_is_always_on(self) -> None:
        """Story #1397: default (0, 0) preserves the pre-#1397 24x7
        behavior (start == end means 'always run')."""
        cfg = HNSWOrphanRepairSweepConfig()
        assert cfg.operating_hours_start_utc == 0
        assert cfg.operating_hours_end_utc == 0

    def test_operating_hours_fields_are_overridable(self) -> None:
        cfg = HNSWOrphanRepairSweepConfig(
            operating_hours_start_utc=22, operating_hours_end_utc=6
        )
        assert cfg.operating_hours_start_utc == 22
        assert cfg.operating_hours_end_utc == 6


class TestServerConfigWiresHNSWOrphanRepairSweepConfig:
    def test_server_config_auto_initializes_when_none(self) -> None:
        config = ServerConfig(server_dir="/tmp/cidx-test-server")
        assert config.hnsw_orphan_repair_sweep_config is not None
        assert isinstance(
            config.hnsw_orphan_repair_sweep_config, HNSWOrphanRepairSweepConfig
        )

    def test_server_config_preserves_explicit_value(self) -> None:
        custom = HNSWOrphanRepairSweepConfig(batch_size=7)
        config = ServerConfig(
            server_dir="/tmp/cidx-test-server",
            hnsw_orphan_repair_sweep_config=custom,
        )
        assert config.hnsw_orphan_repair_sweep_config.batch_size == 7


class TestDictToServerConfigDeserializesHNSWOrphanRepairSweep:
    """Bug #1368: _dict_to_server_config must convert a raw
    hnsw_orphan_repair_sweep_config dict (as loaded from the runtime DB's
    JSON column, PG or SQLite) into a real HNSWOrphanRepairSweepConfig
    instance -- mirroring the sibling activated_reaper_config /
    data_retention_config conversion blocks. Without this conversion, the
    scheduler's `cfg.enabled` / `cfg.batch_size` attribute access raises
    AttributeError on every real cluster/solo deployment, silently caught
    by the scheduler's defensive except-Exception fallback.
    """

    def test_dict_to_server_config_deserializes_hnsw_orphan_repair_sweep_config(
        self, tmp_path
    ) -> None:
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(str(tmp_path))
        config_dict = {
            "server_dir": str(tmp_path),
            "hnsw_orphan_repair_sweep_config": {
                "enabled": False,
                "batch_size": 42,
                "tick_interval_minutes": 3,
            },
        }
        config = manager._dict_to_server_config(config_dict)

        assert isinstance(
            config.hnsw_orphan_repair_sweep_config, HNSWOrphanRepairSweepConfig
        ), (
            "hnsw_orphan_repair_sweep_config must be a real "
            "HNSWOrphanRepairSweepConfig instance, not a plain dict "
            "(Bug #1368: 'dict' object has no attribute 'enabled')"
        )
        assert config.hnsw_orphan_repair_sweep_config.enabled is False
        assert config.hnsw_orphan_repair_sweep_config.batch_size == 42
        assert config.hnsw_orphan_repair_sweep_config.tick_interval_minutes == 3
