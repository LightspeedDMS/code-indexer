"""
Unit tests for MCP Thread Pool Optimization (Story #1009).

Tests verify:
- mcp_dispatch_pool_size defaults to 128 in ServerConfig
- mcp_dispatch_pool_size validation rejects values outside 1-1024
- MultiSearchService.get_instance() returns a singleton
- Subsidiary pool defaults are enlarged (8, 8, 8, 4)
"""

import pytest

# Expected default and bounds for the new bootstrap config key
EXPECTED_MCP_DISPATCH_POOL_SIZE = 128
INVALID_MCP_DISPATCH_POOL_SIZE_BELOW = 0
INVALID_MCP_DISPATCH_POOL_SIZE_ABOVE = 1025

# Singleton test config values
SINGLETON_MAX_WORKERS = 2
SINGLETON_MAX_RESULTS = 10
SINGLETON_TIMEOUT = 30

# Expected enlarged subsidiary pool defaults (Story #1009)
EXPECTED_MULTI_SEARCH_MAX_WORKERS = 8
EXPECTED_SCIP_MULTI_MAX_WORKERS = 8
EXPECTED_SUBPROCESS_MAX_WORKERS = 8
EXPECTED_XRAY_WORKER_THREADS = 4


class TestMcpThreadPool:
    """Tests for MCP thread pool optimization (Story #1009)."""

    def test_mcp_dispatch_pool_size_default(self, tmp_path):
        """mcp_dispatch_pool_size must default to 128 on ServerConfig."""
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir=str(tmp_path))
        assert config.mcp_dispatch_pool_size == EXPECTED_MCP_DISPATCH_POOL_SIZE

    def test_mcp_dispatch_pool_size_validation(self, tmp_path):
        """validate_config must reject mcp_dispatch_pool_size values outside 1-1024."""
        from code_indexer.server.utils.config_manager import (
            ServerConfig,
            ServerConfigManager,
        )

        manager = ServerConfigManager(server_dir_path=tmp_path)

        config_below = ServerConfig(
            server_dir=str(tmp_path),
            mcp_dispatch_pool_size=INVALID_MCP_DISPATCH_POOL_SIZE_BELOW,
        )
        with pytest.raises(ValueError, match="mcp_dispatch_pool_size"):
            manager.validate_config(config_below)

        config_above = ServerConfig(
            server_dir=str(tmp_path),
            mcp_dispatch_pool_size=INVALID_MCP_DISPATCH_POOL_SIZE_ABOVE,
        )
        with pytest.raises(ValueError, match="mcp_dispatch_pool_size"):
            manager.validate_config(config_above)

    def test_multi_search_service_singleton(self):
        """get_instance() must return the same MultiSearchService instance on repeated calls."""
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig

        MultiSearchService._reset_singleton()

        config = MultiSearchConfig(
            max_workers=SINGLETON_MAX_WORKERS,
            max_results_per_repo=SINGLETON_MAX_RESULTS,
            query_timeout_seconds=SINGLETON_TIMEOUT,
        )
        instance1 = MultiSearchService.get_instance(config)
        instance2 = MultiSearchService.get_instance(config)

        assert instance1 is instance2

    def test_subsidiary_pool_defaults(self):
        """multi_search_max_workers, scip_multi_max_workers, subprocess_max_workers, xray_worker_threads must be 8, 8, 8, 4."""
        from code_indexer.server.utils.config_manager import (
            MultiSearchLimitsConfig,
            BackgroundJobsConfig,
            XRayConfig,
        )

        multi = MultiSearchLimitsConfig()
        bg = BackgroundJobsConfig()
        xray = XRayConfig()

        assert multi.multi_search_max_workers == EXPECTED_MULTI_SEARCH_MAX_WORKERS
        assert multi.scip_multi_max_workers == EXPECTED_SCIP_MULTI_MAX_WORKERS
        assert bg.subprocess_max_workers == EXPECTED_SUBPROCESS_MAX_WORKERS
        assert xray.xray_worker_threads == EXPECTED_XRAY_WORKER_THREADS
