"""
Unit tests for multi_query_routes config integration (Story #25).

Tests verify that:
1. get_multi_search_service uses config values from ConfigService
2. MultiSearchConfig.from_config() creates config from ConfigService
3. Services are instantiated with correct worker/timeout values

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest
from unittest.mock import patch, MagicMock

from code_indexer.server.routes.multi_query_routes import (
    get_multi_search_service,
    _multi_search_service,
)
from code_indexer.server.multi import MultiSearchConfig


class TestMultiSearchConfigFromConfig:
    """Test MultiSearchConfig.from_config method (Story #25)."""

    def test_multi_search_config_has_from_config_method(self):
        """MultiSearchConfig should have from_config class method."""
        assert hasattr(MultiSearchConfig, "from_config")
        assert callable(getattr(MultiSearchConfig, "from_config"))

    def test_from_config_returns_multi_search_config(self, tmp_path):
        """from_config should return MultiSearchConfig instance."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        config = MultiSearchConfig.from_config(service)
        assert isinstance(config, MultiSearchConfig)

    def test_from_config_uses_configured_max_workers(self, tmp_path):
        """from_config should use multi_search_max_workers from ConfigService."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("multi_search", "multi_search_max_workers", 8)

        config = MultiSearchConfig.from_config(service)
        assert config.max_workers == 8

    def test_from_config_uses_configured_timeout(self, tmp_path):
        """from_config should use multi_search_timeout_seconds from ConfigService."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service.update_setting("multi_search", "multi_search_timeout_seconds", 90)

        config = MultiSearchConfig.from_config(service)
        assert config.query_timeout_seconds == 90

    def test_from_config_uses_default_values(self, tmp_path):
        """from_config should use default values from ConfigService (2 workers, 30s)."""
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        config = MultiSearchConfig.from_config(service)
        # Per resource audit: defaults are 2 workers, 30s timeout
        assert config.max_workers == 2
        assert config.query_timeout_seconds == 30


class TestGetMultiSearchServiceConfig:
    """Test get_multi_search_service uses ConfigService (Story #25)."""

    def test_get_multi_search_service_uses_config_values(self, tmp_path):
        """get_multi_search_service should use values from ConfigService."""
        from code_indexer.server.services.config_service import (
            ConfigService,
            reset_config_service,
        )
        from code_indexer.server.routes import multi_query_routes

        # Reset global state
        multi_query_routes._multi_search_service = None
        reset_config_service()

        # Create a mock config service with custom values
        with patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_config:
            mock_service = MagicMock()
            mock_config = MagicMock()
            mock_config.multi_search_limits_config.multi_search_max_workers = 6
            mock_config.multi_search_limits_config.multi_search_timeout_seconds = 45
            mock_service.get_config.return_value = mock_config
            mock_get_config.return_value = mock_service

            # Get service
            service = get_multi_search_service()

            # Verify config was used
            assert service.config.max_workers == 6
            assert service.config.query_timeout_seconds == 45

            # Cleanup
            multi_query_routes._multi_search_service = None


class TestSCIPMultiServiceConfig:
    """Test SCIPMultiService config integration (Story #25)."""

    def test_scip_multi_service_accepts_config_values(self):
        """SCIPMultiService should accept max_workers and timeout from config."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService(max_workers=4, query_timeout_seconds=60)

        assert service.max_workers == 4
        assert service.query_timeout_seconds == 60

    def test_scip_multi_service_default_values_match_config_defaults(self):
        """SCIPMultiService defaults should match MultiSearchLimitsConfig defaults."""
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        service = SCIPMultiService()

        # Per resource audit and Story #25: defaults should be 2 workers, 30s timeout
        # Note: This test will fail until we update SCIPMultiService defaults
        assert service.max_workers == 2
        assert service.query_timeout_seconds == 30
