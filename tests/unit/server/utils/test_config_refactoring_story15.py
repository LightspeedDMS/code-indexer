"""
Unit tests for Story #15 - Refactor Misplaced Configuration Settings.

AC1: IndexingConfig dataclass with temporal_stale_threshold_days and indexing_timeout_seconds
"""

import tempfile

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    ServerConfig,
    ScipConfig,
)


class TestIndexingConfig:
    """Test suite for AC1: IndexingConfig dataclass."""

    def test_indexing_config_exists_as_class(self):
        """AC1: IndexingConfig dataclass should exist."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        assert IndexingConfig is not None

    def test_indexing_config_has_temporal_stale_threshold_days(self):
        """AC1: IndexingConfig should have temporal_stale_threshold_days field."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        config = IndexingConfig()
        assert hasattr(config, "temporal_stale_threshold_days")
        assert config.temporal_stale_threshold_days == 7

    def test_indexing_config_has_indexing_timeout_seconds(self):
        """AC1: IndexingConfig should have indexing_timeout_seconds field."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        config = IndexingConfig()
        assert hasattr(config, "indexing_timeout_seconds")
        assert config.indexing_timeout_seconds == 3600

    def test_indexing_config_custom_values(self):
        """AC1: IndexingConfig should accept custom values."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        config = IndexingConfig(
            temporal_stale_threshold_days=14,
            indexing_timeout_seconds=7200,
        )

        assert config.temporal_stale_threshold_days == 14
        assert config.indexing_timeout_seconds == 7200

    def test_server_config_has_indexing_config(self):
        """AC1: ServerConfig should have indexing_config attribute."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "indexing_config")
        assert config.indexing_config is not None
        assert isinstance(config.indexing_config, IndexingConfig)

    def test_save_load_preserves_indexing_config(self):
        """Test IndexingConfig is properly serialized/deserialized."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            original_config = ServerConfig(
                server_dir=tmpdir,
                indexing_config=IndexingConfig(
                    temporal_stale_threshold_days=14,
                    indexing_timeout_seconds=7200,
                ),
            )

            manager.save_config(original_config)
            loaded_config = manager.load_config()

            assert loaded_config is not None
            assert loaded_config.indexing_config is not None
            assert loaded_config.indexing_config.temporal_stale_threshold_days == 14
            assert loaded_config.indexing_config.indexing_timeout_seconds == 7200


class TestScipWorkspaceRetentionDaysMove:
    """Test suite for AC2: Move scip_workspace_retention_days to ScipConfig."""

    def test_scip_config_has_workspace_retention_days(self):
        """AC2: ScipConfig should have scip_workspace_retention_days field."""
        config = ScipConfig()
        assert hasattr(config, "scip_workspace_retention_days")
        assert config.scip_workspace_retention_days == 7

    def test_server_config_no_longer_has_scip_workspace_retention_days(self):
        """AC2: ServerConfig should NOT have scip_workspace_retention_days as loose field."""
        config = ServerConfig(server_dir="/tmp/test")
        # After refactoring, this should NOT be on ServerConfig root level
        assert not hasattr(config, "scip_workspace_retention_days")

    def test_save_load_preserves_scip_workspace_retention_days_in_scip_config(self):
        """Test scip_workspace_retention_days in ScipConfig is properly persisted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            original_config = ServerConfig(
                server_dir=tmpdir,
                scip_config=ScipConfig(
                    scip_workspace_retention_days=14,
                    scip_generation_timeout_seconds=600,
                ),
            )

            manager.save_config(original_config)
            loaded_config = manager.load_config()

            assert loaded_config is not None
            assert loaded_config.scip_config is not None
            assert loaded_config.scip_config.scip_workspace_retention_days == 14


class TestClaudeIntegrationConfig:
    """Test suite for AC3: ClaudeIntegrationConfig dataclass in config_manager."""

    def test_claude_integration_config_exists(self):
        """AC3: ClaudeIntegrationConfig dataclass should exist."""
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        assert ClaudeIntegrationConfig is not None

    def test_claude_integration_config_has_anthropic_api_key(self):
        """AC3: ClaudeIntegrationConfig should have anthropic_api_key field."""
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        config = ClaudeIntegrationConfig()
        assert hasattr(config, "anthropic_api_key")
        assert config.anthropic_api_key is None

    def test_claude_integration_config_has_max_concurrent_claude_cli(self):
        """AC3: ClaudeIntegrationConfig should have max_concurrent_claude_cli field."""
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        config = ClaudeIntegrationConfig()
        assert hasattr(config, "max_concurrent_claude_cli")
        assert config.max_concurrent_claude_cli == 4

    def test_claude_integration_config_has_description_refresh_interval_hours(self):
        """AC3: ClaudeIntegrationConfig should have description_refresh_interval_hours."""
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        config = ClaudeIntegrationConfig()
        assert hasattr(config, "description_refresh_interval_hours")
        assert config.description_refresh_interval_hours == 24

    def test_server_config_has_claude_integration_config(self):
        """AC3: ServerConfig should have claude_integration_config attribute."""
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "claude_integration_config")
        assert config.claude_integration_config is not None
        assert isinstance(config.claude_integration_config, ClaudeIntegrationConfig)

    def test_server_config_no_longer_has_loose_claude_settings(self):
        """AC3: ServerConfig should NOT have loose Claude CLI settings."""
        config = ServerConfig(server_dir="/tmp/test")
        assert not hasattr(config, "anthropic_api_key")
        assert not hasattr(config, "max_concurrent_claude_cli")
        assert not hasattr(config, "description_refresh_interval_hours")


class TestRepositoryConfig:
    """Test suite for AC4: RepositoryConfig dataclass."""

    def test_repository_config_exists(self):
        """AC4: RepositoryConfig dataclass should exist."""
        from code_indexer.server.utils.config_manager import RepositoryConfig

        assert RepositoryConfig is not None

    def test_repository_config_has_enable_pr_creation(self):
        """AC4: RepositoryConfig should have enable_pr_creation field."""
        from code_indexer.server.utils.config_manager import RepositoryConfig

        config = RepositoryConfig()
        assert hasattr(config, "enable_pr_creation")
        assert config.enable_pr_creation is True

    def test_repository_config_has_pr_base_branch(self):
        """AC4: RepositoryConfig should have pr_base_branch field."""
        from code_indexer.server.utils.config_manager import RepositoryConfig

        config = RepositoryConfig()
        assert hasattr(config, "pr_base_branch")
        assert config.pr_base_branch == "main"

    def test_repository_config_has_default_branch(self):
        """AC4: RepositoryConfig should have default_branch field."""
        from code_indexer.server.utils.config_manager import RepositoryConfig

        config = RepositoryConfig()
        assert hasattr(config, "default_branch")
        assert config.default_branch == "main"

    def test_server_config_has_repository_config(self):
        """AC4: ServerConfig should have repository_config attribute."""
        from code_indexer.server.utils.config_manager import RepositoryConfig

        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "repository_config")
        assert config.repository_config is not None
        assert isinstance(config.repository_config, RepositoryConfig)

    def test_server_config_no_longer_has_loose_repository_settings(self):
        """AC4: ServerConfig should NOT have loose repository settings."""
        config = ServerConfig(server_dir="/tmp/test")
        assert not hasattr(config, "enable_pr_creation")
        assert not hasattr(config, "pr_base_branch")
        assert not hasattr(config, "default_branch")
