"""Unit tests for MaintenanceService timeout calculation (Bug #135).

Bug #135: Auto-update drain timeout must be dynamically calculated from server config.

Tests for get_max_job_timeout() and get_recommended_drain_timeout() methods.
"""

import pytest
from code_indexer.server.services.maintenance_service import get_maintenance_state
from code_indexer.server.utils.config_manager import (
    ServerConfig,
    ServerResourceConfig,
    ScipConfig,
)


class TestMaintenanceServiceMaxJobTimeout:
    """Test get_max_job_timeout method that calculates maximum from all config timeouts."""

    def test_returns_max_from_resource_config_timeouts(self):
        """Should return the maximum value from all job-related timeouts in config."""
        # Setup: Create config with known timeout values
        config = ServerConfig(
            server_dir="/tmp/test",
            resource_config=ServerResourceConfig(
                git_refresh_timeout=3600,  # 1 hour (max)
                cidx_index_timeout=1800,  # 30 min
            ),
            scip_config=ScipConfig(
                indexing_timeout_seconds=1800,  # 30 min
                scip_generation_timeout_seconds=600,  # 10 min
            ),
        )

        state = get_maintenance_state()
        max_timeout = state.get_max_job_timeout(config)

        # Should return 3600 (the maximum timeout)
        assert max_timeout == 3600

    def test_includes_scip_indexing_timeout(self):
        """Should include SCIP indexing timeout in calculation."""
        config = ServerConfig(
            server_dir="/tmp/test",
            resource_config=ServerResourceConfig(
                git_refresh_timeout=1800,
                cidx_index_timeout=1800,
            ),
            scip_config=ScipConfig(
                indexing_timeout_seconds=7200,  # 2 hours (max)
                scip_generation_timeout_seconds=600,
            ),
        )

        state = get_maintenance_state()
        max_timeout = state.get_max_job_timeout(config)

        assert max_timeout == 7200

    def test_includes_scip_generation_timeout(self):
        """Should include SCIP generation timeout in calculation."""
        config = ServerConfig(
            server_dir="/tmp/test",
            resource_config=ServerResourceConfig(
                git_refresh_timeout=300,
                cidx_index_timeout=300,
            ),
            scip_config=ScipConfig(
                indexing_timeout_seconds=300,
                scip_generation_timeout_seconds=5400,  # 90 min (max)
            ),
        )

        state = get_maintenance_state()
        max_timeout = state.get_max_job_timeout(config)

        assert max_timeout == 5400

    def test_handles_default_config_values(self):
        """Should work with default config values (all 1 hour)."""
        config = ServerConfig(server_dir="/tmp/test")
        # Default values:
        # git_refresh_timeout = 3600
        # cidx_index_timeout = 3600
        # indexing_timeout_seconds = 3600
        # scip_generation_timeout_seconds = 600

        state = get_maintenance_state()
        max_timeout = state.get_max_job_timeout(config)

        # Should return 3600 (multiple timeouts at this value)
        assert max_timeout == 3600


class TestMaintenanceServiceRecommendedDrainTimeout:
    """Test get_recommended_drain_timeout method (1.5x max job timeout)."""

    def test_returns_one_and_half_times_max_timeout(self):
        """Should return 1.5x the maximum job timeout."""
        config = ServerConfig(
            server_dir="/tmp/test",
            resource_config=ServerResourceConfig(
                git_refresh_timeout=3600,  # 1 hour
                cidx_index_timeout=3600,
            ),
            scip_config=ScipConfig(
                indexing_timeout_seconds=3600,
                scip_generation_timeout_seconds=600,
            ),
        )

        state = get_maintenance_state()
        recommended = state.get_recommended_drain_timeout(config)

        # 3600 * 1.5 = 5400 seconds (90 minutes)
        assert recommended == 5400

    def test_rounds_to_integer(self):
        """Should return integer value even if calculation produces float."""
        config = ServerConfig(
            server_dir="/tmp/test",
            resource_config=ServerResourceConfig(
                git_refresh_timeout=1000,  # Odd number for testing rounding
                cidx_index_timeout=1000,
            ),
            scip_config=ScipConfig(
                indexing_timeout_seconds=1000,
                scip_generation_timeout_seconds=600,
            ),
        )

        state = get_maintenance_state()
        recommended = state.get_recommended_drain_timeout(config)

        # 1000 * 1.5 = 1500
        assert recommended == 1500
        assert isinstance(recommended, int)

    def test_handles_large_timeouts(self):
        """Should handle large timeout values correctly."""
        config = ServerConfig(
            server_dir="/tmp/test",
            resource_config=ServerResourceConfig(
                git_refresh_timeout=7200,  # 2 hours
                cidx_index_timeout=7200,
            ),
            scip_config=ScipConfig(
                indexing_timeout_seconds=7200,
                scip_generation_timeout_seconds=600,
            ),
        )

        state = get_maintenance_state()
        recommended = state.get_recommended_drain_timeout(config)

        # 7200 * 1.5 = 10800 seconds (3 hours)
        assert recommended == 10800
