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
