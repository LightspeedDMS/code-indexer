"""
Unit tests for temporal async-hybrid query config fields (Story #1400).

Covers the FINAL LOCKED DESIGN's CRITICAL 5 (inline-wait/handler-timeout
grace budget, static validation defense-in-depth) and CRITICAL 1
(temporal_lane_concurrency, restart-required) config additions:

- SearchTimeoutsConfig.temporal_inline_wait_seconds: float, default 60.0,
  validated < search_code_handler_timeout_seconds - 1.0 (grace budget).
- BackgroundJobsConfig.temporal_lane_concurrency: int, default 2, validated
  1..32, restart-required (lane pool built at BGM init).

TDD: written BEFORE implementation.
"""

import pytest

from code_indexer.server.utils.config_manager import (
    BackgroundJobsConfig,
    SearchTimeoutsConfig,
    ServerConfig,
    ServerConfigManager,
)
from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS


class TestSearchTimeoutsConfigTemporalField:
    def test_default_temporal_inline_wait_seconds_is_60(self):
        config = SearchTimeoutsConfig()
        assert config.temporal_inline_wait_seconds == 60.0

    def test_temporal_inline_wait_seconds_accepts_float(self):
        config = SearchTimeoutsConfig(temporal_inline_wait_seconds=0.001)
        assert config.temporal_inline_wait_seconds == 0.001

    def test_temporal_inline_wait_seconds_is_float_type(self):
        config = SearchTimeoutsConfig()
        assert isinstance(config.temporal_inline_wait_seconds, float)


class TestBackgroundJobsConfigTemporalLane:
    def test_default_temporal_lane_concurrency_is_2(self):
        config = BackgroundJobsConfig()
        assert config.temporal_lane_concurrency == 2

    def test_temporal_lane_concurrency_custom_value(self):
        config = BackgroundJobsConfig(temporal_lane_concurrency=4)
        assert config.temporal_lane_concurrency == 4

    def test_temporal_lane_concurrency_is_restart_required(self):
        """CRITICAL 1: lane pool is built at BGM init -- restart required."""
        assert "temporal_lane_concurrency" in RESTART_REQUIRED_FIELDS


class TestValidateConfigTemporalInlineWait(object):
    def _config(self, tmp_path) -> ServerConfigManager:
        return ServerConfigManager(server_dir_path=str(tmp_path))

    def test_valid_temporal_inline_wait_seconds_passes(self, tmp_path):
        mgr = self._config(tmp_path)
        config = ServerConfig(server_dir=str(tmp_path))
        config.search_timeouts_config = SearchTimeoutsConfig(
            search_code_handler_timeout_seconds=180,
            temporal_inline_wait_seconds=60.0,
        )
        mgr.validate_config(config)  # must not raise

    def test_temporal_inline_wait_seconds_negative_rejected(self, tmp_path):
        mgr = self._config(tmp_path)
        config = ServerConfig(server_dir=str(tmp_path))
        config.search_timeouts_config = SearchTimeoutsConfig(
            temporal_inline_wait_seconds=-1.0,
        )
        with pytest.raises(ValueError):
            mgr.validate_config(config)

    def test_temporal_inline_wait_seconds_grace_budget_violation_rejected(
        self, tmp_path
    ):
        """CRITICAL 5 (static defense-in-depth): temporal_inline_wait_seconds
        must be <= search_code_handler_timeout_seconds - 1.0 (grace)."""
        mgr = self._config(tmp_path)
        config = ServerConfig(server_dir=str(tmp_path))
        config.search_timeouts_config = SearchTimeoutsConfig(
            search_code_handler_timeout_seconds=180,
            temporal_inline_wait_seconds=179.999,
        )
        with pytest.raises(ValueError):
            mgr.validate_config(config)

    def test_temporal_inline_wait_seconds_at_exact_grace_boundary_passes(
        self, tmp_path
    ):
        mgr = self._config(tmp_path)
        config = ServerConfig(server_dir=str(tmp_path))
        config.search_timeouts_config = SearchTimeoutsConfig(
            search_code_handler_timeout_seconds=180,
            temporal_inline_wait_seconds=179.0,
        )
        mgr.validate_config(config)  # must not raise (exactly at the boundary)


class TestValidateConfigTemporalLaneConcurrency:
    def _config(self, tmp_path) -> ServerConfigManager:
        return ServerConfigManager(server_dir_path=str(tmp_path))

    def test_valid_temporal_lane_concurrency_passes(self, tmp_path):
        mgr = self._config(tmp_path)
        config = ServerConfig(server_dir=str(tmp_path))
        config.background_jobs_config = BackgroundJobsConfig(
            temporal_lane_concurrency=2
        )
        mgr.validate_config(config)  # must not raise

    def test_temporal_lane_concurrency_zero_rejected(self, tmp_path):
        mgr = self._config(tmp_path)
        config = ServerConfig(server_dir=str(tmp_path))
        config.background_jobs_config = BackgroundJobsConfig(
            temporal_lane_concurrency=0
        )
        with pytest.raises(ValueError):
            mgr.validate_config(config)

    def test_temporal_lane_concurrency_above_32_rejected(self, tmp_path):
        mgr = self._config(tmp_path)
        config = ServerConfig(server_dir=str(tmp_path))
        config.background_jobs_config = BackgroundJobsConfig(
            temporal_lane_concurrency=33
        )
        with pytest.raises(ValueError):
            mgr.validate_config(config)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
