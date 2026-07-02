"""
Unit tests for Story #3 Phase 2 - Configuration Consolidation.

Tests for McpSessionConfig, HealthConfig, and ScipConfig dataclasses
and their integration into ServerConfig, ConfigService, and validation.
"""

import tempfile
from unittest.mock import patch

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    McpSessionConfig,
    HealthConfig,
    ScipConfig,
)
from code_indexer.server.services.config_service import ConfigService


class TestMcpSessionConfig:
    """Test suite for McpSessionConfig dataclass (AC2-AC3)."""

    def test_default_values(self):
        """Test McpSessionConfig has correct default values."""
        config = McpSessionConfig()

        assert config.session_ttl_seconds == 3600  # 1 hour
        assert config.cleanup_interval_seconds == 900  # 15 minutes

    def test_custom_values(self):
        """Test McpSessionConfig accepts custom values."""
        config = McpSessionConfig(
            session_ttl_seconds=7200,
            cleanup_interval_seconds=1800,
        )

        assert config.session_ttl_seconds == 7200
        assert config.cleanup_interval_seconds == 1800


class TestHealthConfig:
    """Test suite for HealthConfig dataclass (AC4-AC8)."""

    def test_default_values(self):
        """Test HealthConfig has correct default values."""
        config = HealthConfig()

        assert config.memory_warning_threshold_percent == 80.0
        assert config.memory_critical_threshold_percent == 90.0
        assert config.disk_warning_threshold_percent == 80.0
        assert config.disk_critical_threshold_percent == 90.0
        assert config.cpu_sustained_threshold_percent == 95.0

    def test_custom_values(self):
        """Test HealthConfig accepts custom values."""
        config = HealthConfig(
            memory_warning_threshold_percent=70.0,
            memory_critical_threshold_percent=85.0,
            disk_warning_threshold_percent=75.0,
            disk_critical_threshold_percent=92.0,
            cpu_sustained_threshold_percent=90.0,
        )

        assert config.memory_warning_threshold_percent == 70.0
        assert config.memory_critical_threshold_percent == 85.0
        assert config.disk_warning_threshold_percent == 75.0
        assert config.disk_critical_threshold_percent == 92.0
        assert config.cpu_sustained_threshold_percent == 90.0


class TestScipConfig:
    """Test suite for ScipConfig dataclass (AC9-AC11)."""

    def test_default_values(self):
        """Test ScipConfig has correct default values."""
        config = ScipConfig()

        assert config.indexing_timeout_seconds == 3600  # 1 hour
        assert config.scip_generation_timeout_seconds == 600  # 10 minutes
        assert config.temporal_stale_threshold_days == 7

    def test_custom_values(self):
        """Test ScipConfig accepts custom values."""
        config = ScipConfig(
            indexing_timeout_seconds=7200,
            scip_generation_timeout_seconds=1200,
            temporal_stale_threshold_days=14,
        )

        assert config.indexing_timeout_seconds == 7200
        assert config.scip_generation_timeout_seconds == 1200
        assert config.temporal_stale_threshold_days == 14


class TestServerConfigPhase2Integration:
    """Test suite for ServerConfig integration with Phase 2 config objects."""

    def test_server_config_has_mcp_session_config(self):
        """Test ServerConfig has mcp_session_config attribute initialized."""
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "mcp_session_config")
        assert config.mcp_session_config is not None
        assert isinstance(config.mcp_session_config, McpSessionConfig)

    def test_server_config_has_health_config(self):
        """Test ServerConfig has health_config attribute initialized."""
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "health_config")
        assert config.health_config is not None
        assert isinstance(config.health_config, HealthConfig)

    def test_server_config_has_scip_config(self):
        """Test ServerConfig has scip_config attribute initialized."""
        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "scip_config")
        assert config.scip_config is not None
        assert isinstance(config.scip_config, ScipConfig)

    def test_save_load_preserves_phase2_configs(self):
        """Test Phase 2 configs are properly serialized/deserialized via JSON (H1 fix validation)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a config with custom Phase 2 values
            manager = ServerConfigManager(tmpdir)
            original_config = ServerConfig(
                server_dir=tmpdir,
                mcp_session_config=McpSessionConfig(
                    session_ttl_seconds=5000,
                    cleanup_interval_seconds=600,
                ),
                health_config=HealthConfig(
                    memory_warning_threshold_percent=75.0,
                    memory_critical_threshold_percent=88.0,
                    disk_warning_threshold_percent=70.0,
                    disk_critical_threshold_percent=85.0,
                    cpu_sustained_threshold_percent=92.0,
                ),
                scip_config=ScipConfig(
                    indexing_timeout_seconds=7200,
                    scip_generation_timeout_seconds=1200,
                    temporal_stale_threshold_days=14,
                ),
            )

            # Save and reload
            manager.save_config(original_config)
            loaded_config = manager.load_config()

            # Verify Phase 2 configs are dataclass instances, not dicts
            assert loaded_config is not None

            # Verify mcp_session_config
            assert isinstance(loaded_config.mcp_session_config, McpSessionConfig)
            assert loaded_config.mcp_session_config.session_ttl_seconds == 5000
            assert loaded_config.mcp_session_config.cleanup_interval_seconds == 600

            # Verify health_config
            assert isinstance(loaded_config.health_config, HealthConfig)
            assert loaded_config.health_config.memory_warning_threshold_percent == 75.0
            assert loaded_config.health_config.memory_critical_threshold_percent == 88.0
            assert loaded_config.health_config.disk_warning_threshold_percent == 70.0
            assert loaded_config.health_config.disk_critical_threshold_percent == 85.0
            assert loaded_config.health_config.cpu_sustained_threshold_percent == 92.0

            # Verify scip_config
            assert isinstance(loaded_config.scip_config, ScipConfig)
            assert loaded_config.scip_config.indexing_timeout_seconds == 7200
            assert loaded_config.scip_config.scip_generation_timeout_seconds == 1200
            assert loaded_config.scip_config.temporal_stale_threshold_days == 14


class TestConfigServicePhase2Integration:
    """Test suite for ConfigService integration with Phase 2 settings."""

    def test_get_all_settings_includes_mcp_session(self):
        """Test get_all_settings returns mcp_session category with correct values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)
            settings = service.get_all_settings()

            assert "mcp_session" in settings
            assert settings["mcp_session"]["session_ttl_seconds"] == 3600
            assert settings["mcp_session"]["cleanup_interval_seconds"] == 900

    def test_get_all_settings_includes_health(self):
        """Test get_all_settings returns health category with correct values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)
            settings = service.get_all_settings()

            assert "health" in settings
            assert settings["health"]["memory_warning_threshold_percent"] == 80.0
            assert settings["health"]["memory_critical_threshold_percent"] == 90.0
            assert settings["health"]["disk_warning_threshold_percent"] == 80.0
            assert settings["health"]["disk_critical_threshold_percent"] == 90.0
            assert settings["health"]["cpu_sustained_threshold_percent"] == 95.0

    def test_get_all_settings_includes_scip(self):
        """Test get_all_settings returns scip category with correct values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)
            settings = service.get_all_settings()

            assert "scip" in settings
            assert settings["scip"]["indexing_timeout_seconds"] == 3600
            assert settings["scip"]["scip_generation_timeout_seconds"] == 600
            assert settings["scip"]["temporal_stale_threshold_days"] == 7

    def test_update_mcp_session_setting(self):
        """Test updating MCP session settings via ConfigService."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Update session_ttl_seconds
            service.update_setting("mcp_session", "session_ttl_seconds", 7200)
            settings = service.get_all_settings()
            assert settings["mcp_session"]["session_ttl_seconds"] == 7200

            # Update cleanup_interval_seconds
            service.update_setting("mcp_session", "cleanup_interval_seconds", 1800)
            settings = service.get_all_settings()
            assert settings["mcp_session"]["cleanup_interval_seconds"] == 1800

    def test_update_health_setting(self):
        """Test updating health monitoring settings via ConfigService."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Update memory thresholds
            service.update_setting("health", "memory_warning_threshold_percent", 75.0)
            settings = service.get_all_settings()
            assert settings["health"]["memory_warning_threshold_percent"] == 75.0

            # Update CPU threshold
            service.update_setting("health", "cpu_sustained_threshold_percent", 92.0)
            settings = service.get_all_settings()
            assert settings["health"]["cpu_sustained_threshold_percent"] == 92.0

    def test_update_scip_setting(self):
        """Test updating SCIP settings via ConfigService."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = ConfigService(tmpdir)

            # Update indexing_timeout_seconds
            service.update_setting("scip", "indexing_timeout_seconds", 7200)
            settings = service.get_all_settings()
            assert settings["scip"]["indexing_timeout_seconds"] == 7200

            # Update temporal_stale_threshold_days
            service.update_setting("scip", "temporal_stale_threshold_days", 14)
            settings = service.get_all_settings()
            assert settings["scip"]["temporal_stale_threshold_days"] == 14


class TestSessionRegistryConfigIntegration:
    """Test suite for SessionRegistry reading from ConfigService (AC2-AC3)."""

    def test_uses_config_service_values(self):
        """Test SessionRegistry uses ConfigService values when not explicitly provided."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up ConfigService with custom values
            service = ConfigService(tmpdir)
            service.update_setting("mcp_session", "session_ttl_seconds", 5000)
            service.update_setting("mcp_session", "cleanup_interval_seconds", 600)

            # Mock get_config_service to return our service
            with patch(
                "code_indexer.server.mcp.session_registry.get_config_service"
            ) as mock_get_config:
                mock_get_config.return_value = service

                # Import and reset singleton for fresh test
                from code_indexer.server.mcp.session_registry import SessionRegistry

                # Create fresh instance by clearing singleton
                SessionRegistry._instance = None
                registry = SessionRegistry()

                # Start cleanup without explicit values - should read from config
                # We need to mock asyncio.create_task since we're not in async context
                with patch("asyncio.create_task"):
                    registry.start_background_cleanup()

                # Verify config values were used
                assert registry._ttl_seconds == 5000
                assert registry._cleanup_interval_seconds == 600


class TestHealthServiceConfigIntegration:
    """Test suite for HealthCheckService reading from ConfigService (AC4-AC8)."""

    def test_uses_config_service_values(self):
        """Test HealthCheckService uses ConfigService values for thresholds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up ConfigService with custom health values
            service = ConfigService(tmpdir)
            service.update_setting("health", "memory_warning_threshold_percent", 70.0)
            service.update_setting("health", "memory_critical_threshold_percent", 85.0)
            service.update_setting("health", "disk_warning_threshold_percent", 75.0)
            service.update_setting("health", "disk_critical_threshold_percent", 92.0)
            service.update_setting("health", "cpu_sustained_threshold_percent", 90.0)

            # Mock get_config_service to return our service
            with patch(
                "code_indexer.server.services.health_service.get_config_service"
            ) as mock_get_config:
                mock_get_config.return_value = service

                # Import and access the module-level thresholds after they're updated
                from code_indexer.server.services import health_service

                # Force reload to pick up mocked config
                health_service._load_thresholds_from_config()

                # Verify config values were used
                assert health_service.MEMORY_WARNING_THRESHOLD == 70.0
                assert health_service.MEMORY_CRITICAL_THRESHOLD == 85.0
                assert health_service.DISK_WARNING_THRESHOLD_PERCENT == 75.0
                assert health_service.DISK_CRITICAL_THRESHOLD_PERCENT == 92.0
                assert health_service.CPU_SUSTAINED_THRESHOLD == 90.0


class TestActivatedRepoIndexManagerConfigIntegration:
    """Test suite for ActivatedRepoIndexManager reading from ConfigService (AC9-AC11)."""

    def test_uses_config_service_values(self):
        """Test ActivatedRepoIndexManager uses ConfigService values for SCIP settings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up ConfigService with custom SCIP values
            service = ConfigService(tmpdir)
            service.update_setting("scip", "indexing_timeout_seconds", 7200)
            service.update_setting("scip", "scip_generation_timeout_seconds", 1200)
            service.update_setting("scip", "temporal_stale_threshold_days", 14)

            # Mock get_config_service to return our service
            with patch(
                "code_indexer.server.services.activated_repo_index_manager.get_config_service"
            ) as mock_get_config:
                mock_get_config.return_value = service

                # Import and create manager
                from code_indexer.server.services.activated_repo_index_manager import (
                    ActivatedRepoIndexManager,
                )

                manager = ActivatedRepoIndexManager(data_dir=tmpdir)

                # Verify config values were used
                assert manager.INDEXING_TIMEOUT_SECONDS == 7200
                assert manager.SCIP_TIMEOUT_SECONDS == 1200
                assert manager.STALE_THRESHOLD_DAYS == 14
