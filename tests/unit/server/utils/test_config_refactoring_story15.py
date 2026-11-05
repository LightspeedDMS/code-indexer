"""
Unit tests for Story #15 - Refactor Misplaced Configuration Settings.

AC1: IndexingConfig dataclass.

NOTE (Story #683 AC1): temporal_stale_threshold_days and indexing_timeout_seconds
were duplicated in IndexingConfig and ScipConfig. Story #683 removed them from
IndexingConfig — ScipConfig is now the canonical location for these fields.
Tests updated accordingly.
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

    def test_indexing_config_does_not_have_temporal_stale_threshold_days(self):
        """Story #683 AC1: temporal_stale_threshold_days removed from IndexingConfig (canonical in ScipConfig)."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        config = IndexingConfig()
        assert not hasattr(config, "temporal_stale_threshold_days")

    def test_indexing_config_does_not_have_indexing_timeout_seconds(self):
        """Story #683 AC1: indexing_timeout_seconds removed from IndexingConfig (canonical in ScipConfig)."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        config = IndexingConfig()
        assert not hasattr(config, "indexing_timeout_seconds")

    def test_scip_config_has_temporal_stale_threshold_days(self):
        """Story #683 AC1: temporal_stale_threshold_days canonical location is ScipConfig."""
        config = ScipConfig()
        assert hasattr(config, "temporal_stale_threshold_days")
        assert config.temporal_stale_threshold_days == 7

    def test_scip_config_does_not_have_indexing_timeout_seconds(self):
        """Bug #1218: indexing_timeout_seconds was removed from ScipConfig.
        Overarching job timeouts caused large-repo indexing to be killed mid-flight."""
        config = ScipConfig()
        assert not hasattr(config, "indexing_timeout_seconds")

    def test_server_config_has_indexing_config(self):
        """AC1: ServerConfig should have indexing_config attribute."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        config = ServerConfig(server_dir="/tmp/test")

        assert hasattr(config, "indexing_config")
        assert config.indexing_config is not None
        assert isinstance(config.indexing_config, IndexingConfig)

    def test_save_load_preserves_indexing_config(self):
        """Test IndexingConfig (without removed fields) is properly serialized/deserialized."""
        from code_indexer.server.utils.config_manager import IndexingConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            original_config = ServerConfig(
                server_dir=tmpdir,
                indexing_config=IndexingConfig(),
                scip_config=ScipConfig(
                    temporal_stale_threshold_days=14,
                ),
            )

            manager.save_config(original_config)
            loaded_config = manager.load_config()

            assert loaded_config is not None
            assert loaded_config.indexing_config is not None
            assert loaded_config.scip_config.temporal_stale_threshold_days == 14


class TestScipWorkspaceRetentionDaysMove:
    """AC2 moved scip_workspace_retention_days into ScipConfig; since modular
    phase 4d it is the cidx-scip feature's setting (feature_settings["scip"]),
    and a stored value is carried there on load."""

    def test_scip_config_no_longer_has_workspace_retention_days(self):
        """ScipConfig keeps only the core's temporal staleness threshold."""
        config = ScipConfig()
        assert not hasattr(config, "scip_workspace_retention_days")
        assert config.temporal_stale_threshold_days == 7

    def test_server_config_no_longer_has_scip_workspace_retention_days(self):
        """AC2: ServerConfig should NOT have scip_workspace_retention_days as loose field."""
        config = ServerConfig(server_dir="/tmp/test")
        # After refactoring, this should NOT be on ServerConfig root level
        assert not hasattr(config, "scip_workspace_retention_days")

    def test_stored_retention_in_scip_config_moves_to_the_feature_section(self):
        """A stored scip_config.scip_workspace_retention_days is carried into
        feature_settings["scip"] when the configuration loads."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            loaded = manager._dict_to_server_config(
                {
                    "server_dir": tmpdir,
                    "scip_config": {
                        "temporal_stale_threshold_days": 9,
                        "scip_workspace_retention_days": 14,
                    },
                }
            )

            assert loaded.scip_config is not None
            assert loaded.scip_config.temporal_stale_threshold_days == 9
            assert loaded.feature_settings["scip"] == {
                "scip_workspace_retention_days": 14
            }

    def test_legacy_top_level_retention_moves_to_the_feature_section(self):
        """The pre-Story #15 top-level key lands in the same place."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ServerConfigManager(tmpdir)
            loaded = manager._dict_to_server_config(
                {"server_dir": tmpdir, "scip_workspace_retention_days": 21}
            )

            assert loaded.feature_settings["scip"] == {
                "scip_workspace_retention_days": 21
            }


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
        """AC3: ClaudeIntegrationConfig should have max_concurrent_claude_cli field.

        Note: Story #24 changed default from 4 to 2 for resource-constrained systems.
        """
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        config = ClaudeIntegrationConfig()
        assert hasattr(config, "max_concurrent_claude_cli")
        # Story #24: Default changed from 4 to 2 for resource-constrained systems
        assert config.max_concurrent_claude_cli == 2

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
