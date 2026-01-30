"""
TDD tests for Story #29: Consolidate Multi-Repo Search and Add Config Section Documentation.

Tests written FIRST before implementation (TDD methodology).

Acceptance Criteria covered:
- AC1: MCP Multi-Repo Search Uses MultiSearchService
- AC3: Unified Multi-Search Configuration (merge OmniSearchConfig into MultiSearchLimitsConfig)
- AC4: OmniSearchConfig Completely Removed

These tests will FAIL initially and guide the implementation.
"""

import json
import pytest
import tempfile
from pathlib import Path


class TestAC1_MCPMultiRepoSearchUsesMultiSearchService:
    """
    AC1: MCP Multi-Repo Search Uses MultiSearchService.

    The _omni_search_code function in handlers.py should use MultiSearchService
    instead of its own sequential loop implementation.
    """

    def test_omni_search_calls_multi_search_service(self):
        """
        Verify that _omni_search_code uses parallel execution.

        This test verifies the implementation uses parallel execution
        (asyncio.gather or ThreadPoolExecutor) instead of a sequential loop.
        """
        # Import the handler module and verify integration
        from code_indexer.server.mcp import handlers
        import inspect

        # Get the source code of _omni_search_code
        source = inspect.getsource(handlers._omni_search_code)

        # The implementation should use MultiSearchService imports for config
        assert "MultiSearchService" in source or "multi_search" in source, \
            "_omni_search_code should import from multi_search module for parallel execution config"

        # Should use asyncio.gather for parallel execution
        assert "asyncio.gather" in source, \
            "_omni_search_code should use asyncio.gather for parallel execution"

        # Should NOT have the old sequential pattern:
        # "for repo_alias in repo_aliases:\n        try:\n            # Build single-repo params"
        # This specific pattern indicates sequential execution
        old_sequential_pattern = "for repo_alias in repo_aliases:\n        try:\n            # Build single-repo params"
        assert old_sequential_pattern not in source, \
            "_omni_search_code should NOT use sequential for loop with search_code calls"

    def test_omni_search_imports_multi_search_service(self):
        """Verify handlers.py imports MultiSearchService."""
        from code_indexer.server.mcp import handlers
        import inspect

        source = inspect.getsource(handlers)

        # Should import MultiSearchService
        assert "MultiSearchService" in source, \
            "handlers.py should import MultiSearchService"

    def test_omni_search_uses_parallel_execution(self):
        """
        Verify _omni_search_code uses parallel execution (not sequential).

        The implementation should use asyncio.gather for parallel execution.
        """
        from code_indexer.server.mcp import handlers
        import inspect

        source = inspect.getsource(handlers._omni_search_code)

        # The implementation should use parallel execution patterns:
        # Either asyncio.gather, ThreadPoolExecutor, or similar

        # Check for parallel execution pattern
        has_asyncio_gather = "asyncio.gather" in source
        has_thread_executor = "ThreadPoolExecutor" in source

        assert has_asyncio_gather or has_thread_executor, \
            "_omni_search_code should use asyncio.gather or ThreadPoolExecutor for parallel execution"

        # The implementation creates tasks and executes them concurrently
        assert "tasks" in source, \
            "_omni_search_code should create tasks for concurrent execution"


class TestAC3_UnifiedMultiSearchConfiguration:
    """
    AC3: Unified Multi-Search Configuration.

    OmniSearchConfig settings should be merged INTO MultiSearchLimitsConfig.
    All previous OmniSearchConfig functionality must be preserved.
    """

    def test_multi_search_limits_config_has_omni_search_fields(self):
        """
        MultiSearchLimitsConfig should have all OmniSearchConfig fields merged in.

        Merged fields:
        - max_workers (renamed from omni max_workers, default: 10 for omni operations)
        - per_repo_timeout_seconds (default: 300)
        - cache_max_entries (default: 100)
        - cache_ttl_seconds (default: 300)
        - default_limit (default: 10)
        - max_limit (default: 1000)
        - default_aggregation_mode (default: "global")
        - max_results_per_repo (default: 100)
        - max_total_results_before_aggregation (default: 10000)
        - pattern_metacharacters (default: "*?[]^$+|")
        """
        from code_indexer.server.utils.config_manager import MultiSearchLimitsConfig

        config = MultiSearchLimitsConfig()

        # Original MultiSearchLimitsConfig fields
        assert hasattr(config, "multi_search_max_workers")
        assert hasattr(config, "multi_search_timeout_seconds")
        assert hasattr(config, "scip_multi_max_workers")
        assert hasattr(config, "scip_multi_timeout_seconds")

        # Merged from OmniSearchConfig (now prefixed with omni_)
        assert hasattr(config, "omni_max_workers"), \
            "MultiSearchLimitsConfig should have omni_max_workers (merged from OmniSearchConfig)"
        assert hasattr(config, "omni_per_repo_timeout_seconds"), \
            "MultiSearchLimitsConfig should have omni_per_repo_timeout_seconds"
        assert hasattr(config, "omni_cache_max_entries"), \
            "MultiSearchLimitsConfig should have omni_cache_max_entries"
        assert hasattr(config, "omni_cache_ttl_seconds"), \
            "MultiSearchLimitsConfig should have omni_cache_ttl_seconds"
        assert hasattr(config, "omni_default_limit"), \
            "MultiSearchLimitsConfig should have omni_default_limit"
        assert hasattr(config, "omni_max_limit"), \
            "MultiSearchLimitsConfig should have omni_max_limit"
        assert hasattr(config, "omni_default_aggregation_mode"), \
            "MultiSearchLimitsConfig should have omni_default_aggregation_mode"
        assert hasattr(config, "omni_max_results_per_repo"), \
            "MultiSearchLimitsConfig should have omni_max_results_per_repo"
        assert hasattr(config, "omni_max_total_results_before_aggregation"), \
            "MultiSearchLimitsConfig should have omni_max_total_results_before_aggregation"
        assert hasattr(config, "omni_pattern_metacharacters"), \
            "MultiSearchLimitsConfig should have omni_pattern_metacharacters"

    def test_multi_search_limits_config_default_values(self):
        """Verify merged OmniSearchConfig fields have correct default values."""
        from code_indexer.server.utils.config_manager import MultiSearchLimitsConfig

        config = MultiSearchLimitsConfig()

        # OmniSearchConfig defaults (merged fields with omni_ prefix)
        assert config.omni_max_workers == 10
        assert config.omni_per_repo_timeout_seconds == 300
        assert config.omni_cache_max_entries == 100
        assert config.omni_cache_ttl_seconds == 300
        assert config.omni_default_limit == 10
        assert config.omni_max_limit == 1000
        assert config.omni_default_aggregation_mode == "global"
        assert config.omni_max_results_per_repo == 100
        assert config.omni_max_total_results_before_aggregation == 10000
        assert config.omni_pattern_metacharacters == "*?[]^$+|"

    def test_server_config_no_longer_has_omni_search_config(self):
        """
        ServerConfig should NOT have omni_search_config field after consolidation.
        All omni-search settings are now in multi_search_limits_config.
        """
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir="/tmp/test")

        # After consolidation, omni_search_config should be removed
        assert not hasattr(config, "omni_search_config"), \
            "ServerConfig should NOT have omni_search_config (consolidated into multi_search_limits_config)"

    def test_config_service_exposes_omni_settings_via_multi_search(self):
        """
        ConfigService.get_all_settings() should expose omni settings
        in the multi_search section (not a separate omni_search section).
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            from code_indexer.server.services.config_service import ConfigService

            service = ConfigService(server_dir_path=temp_dir)
            service.load_config()
            settings = service.get_all_settings()

            # omni_search section should NOT exist (consolidated)
            assert "omni_search" not in settings, \
                "get_all_settings should NOT have separate omni_search section"

            # multi_search section should have the omni settings
            assert "multi_search" in settings
            multi_search = settings["multi_search"]

            assert "omni_max_workers" in multi_search
            assert "omni_per_repo_timeout_seconds" in multi_search
            assert "omni_cache_max_entries" in multi_search
            assert "omni_cache_ttl_seconds" in multi_search
            assert "omni_default_limit" in multi_search
            assert "omni_max_limit" in multi_search
            assert "omni_default_aggregation_mode" in multi_search
            assert "omni_max_results_per_repo" in multi_search
            assert "omni_max_total_results_before_aggregation" in multi_search
            assert "omni_pattern_metacharacters" in multi_search

    def test_config_service_update_omni_settings_via_multi_search(self):
        """
        ConfigService should allow updating omni settings via multi_search category.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            from code_indexer.server.services.config_service import ConfigService

            service = ConfigService(server_dir_path=temp_dir)
            service.load_config()

            # Should be able to update omni settings via multi_search category
            service.update_setting(
                category="multi_search",
                key="omni_max_workers",
                value=20,
            )

            settings = service.get_all_settings()
            assert settings["multi_search"]["omni_max_workers"] == 20

    def test_old_omni_search_config_migrates_to_multi_search(self, tmp_path):
        """
        Old config.json with omni_search_config should migrate to multi_search_limits_config.
        """
        from code_indexer.server.utils.config_manager import ServerConfigManager

        # Create old-style config with omni_search_config
        old_config_data = {
            "server_dir": str(tmp_path),
            "host": "127.0.0.1",
            "port": 8000,
            "omni_search_config": {
                "max_workers": 15,
                "per_repo_timeout_seconds": 450,
                "cache_max_entries": 150,
                "cache_ttl_seconds": 450,
                "default_limit": 15,
                "max_limit": 1500,
                "default_aggregation_mode": "per_repo",
                "max_results_per_repo": 150,
                "max_total_results_before_aggregation": 15000,
                "pattern_metacharacters": "*?",
            },
        }

        config_file = tmp_path / "config.json"
        with open(config_file, "w") as f:
            json.dump(old_config_data, f)

        # Load - should migrate to multi_search_limits_config
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.load_config()

        # Should NOT have omni_search_config
        assert not hasattr(config, "omni_search_config") or config.omni_search_config is None

        # Should have migrated values in multi_search_limits_config
        assert config.multi_search_limits_config is not None
        assert config.multi_search_limits_config.omni_max_workers == 15
        assert config.multi_search_limits_config.omni_per_repo_timeout_seconds == 450
        assert config.multi_search_limits_config.omni_default_aggregation_mode == "per_repo"


class TestAC4_OmniSearchConfigCompletelyRemoved:
    """
    AC4: OmniSearchConfig Completely Removed.

    After consolidation:
    - No OmniSearchConfig class in source code
    - No references to OmniSearchConfig in tests
    - No omni_search_config field on ServerConfig
    """

    def test_omni_search_config_class_does_not_exist(self):
        """OmniSearchConfig class should not exist after consolidation."""
        from code_indexer.server.utils import config_manager

        # OmniSearchConfig should not be exported
        assert not hasattr(config_manager, "OmniSearchConfig"), \
            "OmniSearchConfig class should be removed from config_manager"

    def test_server_config_does_not_import_omni_search_config(self):
        """ServerConfig should not import or reference OmniSearchConfig."""
        import inspect
        from code_indexer.server.utils import config_manager

        source = inspect.getsource(config_manager)

        # The class definition should not exist
        assert "class OmniSearchConfig" not in source, \
            "OmniSearchConfig class definition should be removed"

    def test_config_service_does_not_reference_omni_search_category(self):
        """ConfigService should not have omni_search as a separate category."""
        import inspect
        from code_indexer.server.services import config_service

        source = inspect.getsource(config_service)

        # Should not have separate _update_omni_search_setting method
        assert "def _update_omni_search_setting" not in source, \
            "_update_omni_search_setting should be removed (merged into _update_multi_search_setting)"

    def test_no_omni_search_config_in_server_config_fields(self):
        """ServerConfig dataclass should not have omni_search_config field."""
        from code_indexer.server.utils.config_manager import ServerConfig
        import dataclasses

        field_names = [f.name for f in dataclasses.fields(ServerConfig)]

        assert "omni_search_config" not in field_names, \
            "ServerConfig should not have omni_search_config field"


class TestAC3_ValidationOfMergedConfig:
    """
    AC3 Validation: Merged configuration should have proper validation.
    """

    def test_validation_accepts_valid_omni_max_workers(self, tmp_path):
        """Valid omni_max_workers values (1-100) should pass validation."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        # Test boundary values
        for workers in [1, 50, 100]:
            config.multi_search_limits_config.omni_max_workers = workers
            # Should not raise
            config_manager.validate_config(config)

    def test_validation_rejects_invalid_omni_max_workers(self, tmp_path):
        """Invalid omni_max_workers should fail validation."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.omni_max_workers = 0

        with pytest.raises(ValueError, match="omni_max_workers"):
            config_manager.validate_config(config)

    def test_validation_accepts_valid_omni_aggregation_mode(self, tmp_path):
        """Valid omni_default_aggregation_mode values should pass validation."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        for mode in ["global", "per_repo"]:
            config.multi_search_limits_config.omni_default_aggregation_mode = mode
            # Should not raise
            config_manager.validate_config(config)

    def test_validation_rejects_invalid_omni_aggregation_mode(self, tmp_path):
        """Invalid omni_default_aggregation_mode should fail validation."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()

        config.multi_search_limits_config.omni_default_aggregation_mode = "invalid"

        with pytest.raises(ValueError, match="omni_default_aggregation_mode"):
            config_manager.validate_config(config)


class TestWebUIConfigSectionNoOmniSearchSection:
    """
    Tests verifying Web UI does NOT have duplicate Omni-Search section
    after consolidation.
    """

    def test_config_section_has_no_omni_search_section(self):
        """
        Web UI config_section.html should NOT have a separate Omni-Search section.
        After consolidation, omni settings are in Multi-Search Settings section.
        """
        config_section_path = Path(
            "/home/jsbattig/Dev/code-indexer/src/code_indexer/server/web/templates/partials/config_section.html"
        )

        if config_section_path.exists():
            content = config_section_path.read_text()

            # Should NOT have separate Omni-Search section
            assert "section-omni-search" not in content, \
                "Web UI should NOT have separate omni-search section (consolidated into multi-search)"
            assert '<h2>Omni-Search Configuration</h2>' not in content, \
                "Web UI should NOT have Omni-Search Configuration header"
