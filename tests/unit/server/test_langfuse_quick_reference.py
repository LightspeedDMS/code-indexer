"""
Unit tests for Langfuse quick reference dynamic content (Story #169).

Tests the _build_langfuse_section helper function that generates
dynamic documentation for Langfuse trace search when pull is enabled.
"""

import json
from dataclasses import dataclass, field
from typing import List, Optional
from unittest.mock import Mock

# Import the function we'll be testing (will fail until implemented)
from code_indexer.server.mcp.handlers import _build_langfuse_section


# Mock dataclasses to match the actual config structure
@dataclass
class LangfusePullProject:
    """Mock for LangfusePullProject."""

    public_key: str = ""
    secret_key: str = ""


@dataclass
class LangfuseConfig:
    """Mock for LangfuseConfig."""

    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    host: str = "https://cloud.langfuse.com"
    auto_trace_enabled: bool = False
    pull_enabled: bool = False
    pull_host: str = "https://cloud.langfuse.com"
    pull_projects: List[LangfusePullProject] = field(default_factory=list)
    pull_sync_interval_seconds: int = 300
    pull_trace_age_days: int = 30


@dataclass
class ServerConfig:
    """Mock for ServerConfig."""

    langfuse_config: Optional[LangfuseConfig] = None
    service_display_name: str = "Neo"


class TestBuildLangfuseSection:
    """Test suite for _build_langfuse_section helper function."""

    def test_langfuse_section_not_included_when_disabled(self):
        """AC1: When pull_enabled is false, return None."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=False,  # Pull disabled
                pull_projects=[],
            )
        )
        golden_repo_manager = Mock()

        result = _build_langfuse_section(config, golden_repo_manager)

        assert result is None

    def test_langfuse_section_not_included_when_no_config(self):
        """AC1: When langfuse_config is None, return None."""
        config = ServerConfig(langfuse_config=None)
        golden_repo_manager = Mock()

        result = _build_langfuse_section(config, golden_repo_manager)

        assert result is None

    def test_langfuse_section_included_when_enabled(self):
        """AC1: When pull_enabled is true, include langfuse_trace_search field."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,  # Pull enabled
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert result is not None
        assert isinstance(result, dict)

    def test_langfuse_section_has_folder_naming(self):
        """AC2: Section includes folder_naming with pattern and full_path."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "folder_naming" in result
        assert "pattern" in result["folder_naming"]
        assert "full_path" in result["folder_naming"]
        assert result["folder_naming"]["pattern"] == "langfuse_<project>_<userId>"
        assert (
            result["folder_naming"]["full_path"]
            == "langfuse_<project>_<userId>/<sessionId>/<traceId>.json"
        )

    def test_langfuse_section_has_search_instructions(self):
        """AC3: Section includes search_instructions with wildcard patterns."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "search_instructions" in result
        assert isinstance(result["search_instructions"], list)
        assert len(result["search_instructions"]) > 0
        # Verify wildcard pattern is mentioned
        assert any(
            "langfuse_*" in instruction for instruction in result["search_instructions"]
        )

    def test_langfuse_section_lists_available_repos(self):
        """AC4: Section dynamically lists actual Langfuse repo aliases."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = [
            {"alias": "langfuse_project1_user123"},
            {"alias": "langfuse_project2_user456"},
            {"alias": "regular_repo"},  # Should be filtered out
        ]

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "available_repositories" in result
        assert isinstance(result["available_repositories"], list)
        assert len(result["available_repositories"]) == 2
        assert "langfuse_project1_user123" in result["available_repositories"]
        assert "langfuse_project2_user456" in result["available_repositories"]
        assert "regular_repo" not in result["available_repositories"]

    def test_langfuse_section_with_no_repos(self):
        """AC4: Empty available_repositories when no golden repos exist."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "available_repositories" in result
        assert result["available_repositories"] == []

    def test_langfuse_section_filters_only_langfuse_repos(self):
        """AC4: Only repos with alias starting with 'langfuse_' are included."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = [
            {"alias": "langfuse_proj_user1"},
            {"alias": "code-indexer"},
            {"alias": "langfuse_another_user2"},
            {"alias": "my-project"},
        ]

        result = _build_langfuse_section(config, golden_repo_manager)

        assert len(result["available_repositories"]) == 2
        assert "langfuse_proj_user1" in result["available_repositories"]
        assert "langfuse_another_user2" in result["available_repositories"]

    def test_langfuse_section_has_example_queries(self):
        """AC6: Section includes practical example queries."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "example_queries" in result
        assert isinstance(result["example_queries"], list)
        assert len(result["example_queries"]) > 0
        # Verify examples use search_code function
        assert any("search_code" in example for example in result["example_queries"])

    def test_langfuse_section_includes_description(self):
        """AC5: Section includes a description field."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "description" in result
        assert isinstance(result["description"], str)
        assert len(result["description"]) > 0

    def test_langfuse_section_includes_configured_projects_count(self):
        """AC5: Section includes count of configured projects."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                    LangfusePullProject(public_key="pk2", secret_key="sk2"),
                    LangfusePullProject(public_key="pk3", secret_key="sk3"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "configured_projects_count" in result
        assert result["configured_projects_count"] == 3

    def test_langfuse_section_handles_none_golden_repo_manager(self):
        """AC5: Handle gracefully when golden_repo_manager is None."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = None  # No manager available

        result = _build_langfuse_section(config, golden_repo_manager)

        # Should still return a result, just with empty repos
        assert result is not None
        assert "available_repositories" in result
        assert result["available_repositories"] == []

    def test_langfuse_section_has_tips(self):
        """AC5: Section includes helpful tips."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, golden_repo_manager)

        assert "tips" in result
        assert isinstance(result["tips"], list)
        assert len(result["tips"]) > 0

    def test_langfuse_section_handles_list_repos_exception(self):
        """Gracefully handle when list_golden_repos raises."""
        config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        golden_repo_manager = Mock()
        golden_repo_manager.list_golden_repos.side_effect = RuntimeError("DB locked")

        result = _build_langfuse_section(config, golden_repo_manager)

        assert result is not None
        assert result["available_repositories"] == []


class TestQuickReferenceIntegration:
    """
    Integration tests for quick_reference handler with Langfuse section.

    Tests the full handler, not just the helper function.
    """

    def test_quick_reference_excludes_langfuse_when_disabled(self, monkeypatch):
        """AC7: quick_reference excludes langfuse_trace_search when pull_enabled is False."""
        # Mock config service
        mock_config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=False,  # Pull disabled
            )
        )
        mock_config_service = Mock()
        mock_config_service.get_config.return_value = mock_config

        # Patch config service
        import code_indexer.server.mcp.handlers as handlers_module

        monkeypatch.setattr(
            handlers_module,
            "get_config_service",
            lambda: mock_config_service,
        )

        # Mock user with admin permissions
        mock_user = Mock()
        mock_user.has_permission.return_value = True

        # Call handler
        from code_indexer.server.mcp.handlers import quick_reference

        result = quick_reference({}, mock_user)

        # Verify response structure
        assert "content" in result
        assert len(result["content"]) > 0
        content_text = result["content"][0]["text"]
        response_data = json.loads(content_text)

        # Verify no langfuse section
        assert "langfuse_trace_search" not in response_data

    def test_quick_reference_includes_langfuse_when_enabled(self, monkeypatch):
        """AC7: quick_reference includes langfuse_trace_search when pull_enabled is True."""
        # Mock config service
        mock_config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,  # Pull enabled
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        mock_config_service = Mock()
        mock_config_service.get_config.return_value = mock_config

        # Mock golden repo manager
        mock_golden_repo_manager = Mock()
        mock_golden_repo_manager.list_golden_repos.return_value = [
            {"alias": "langfuse_project1_user123"},
            {"alias": "regular_repo"},
        ]

        # Patch config service and app_module
        import code_indexer.server.mcp.handlers as handlers_module

        monkeypatch.setattr(
            handlers_module,
            "get_config_service",
            lambda: mock_config_service,
        )
        monkeypatch.setattr(
            handlers_module.app_module,
            "golden_repo_manager",
            mock_golden_repo_manager,
        )

        # Mock user with admin permissions
        mock_user = Mock()
        mock_user.has_permission.return_value = True

        # Call handler
        from code_indexer.server.mcp.handlers import quick_reference

        result = quick_reference({}, mock_user)

        # Verify response structure
        assert "content" in result
        assert len(result["content"]) > 0
        content_text = result["content"][0]["text"]
        response_data = json.loads(content_text)

        # Verify langfuse section is present
        assert "langfuse_trace_search" in response_data
        langfuse_section = response_data["langfuse_trace_search"]

        # Verify section structure
        assert "description" in langfuse_section
        assert "folder_naming" in langfuse_section
        assert "search_instructions" in langfuse_section
        assert "available_repositories" in langfuse_section
        assert "example_queries" in langfuse_section
        assert "tips" in langfuse_section

        # Verify available repositories are filtered correctly
        assert len(langfuse_section["available_repositories"]) == 1
        assert "langfuse_project1_user123" in langfuse_section["available_repositories"]
        assert "regular_repo" not in langfuse_section["available_repositories"]

    def test_quick_reference_preserves_existing_fields(self, monkeypatch):
        """AC7: Langfuse section doesn't disrupt existing response fields."""
        # Mock config service
        mock_config = ServerConfig(
            langfuse_config=LangfuseConfig(
                enabled=True,
                pull_enabled=True,
                pull_projects=[
                    LangfusePullProject(public_key="pk1", secret_key="sk1"),
                ],
            )
        )
        mock_config.service_display_name = "TestServer"
        mock_config_service = Mock()
        mock_config_service.get_config.return_value = mock_config

        # Mock golden repo manager
        mock_golden_repo_manager = Mock()
        mock_golden_repo_manager.list_golden_repos.return_value = []

        # Patch config service and app_module
        import code_indexer.server.mcp.handlers as handlers_module

        monkeypatch.setattr(
            handlers_module,
            "get_config_service",
            lambda: mock_config_service,
        )
        monkeypatch.setattr(
            handlers_module.app_module,
            "golden_repo_manager",
            mock_golden_repo_manager,
        )

        # Mock user with admin permissions
        mock_user = Mock()
        mock_user.has_permission.return_value = True

        # Call handler
        from code_indexer.server.mcp.handlers import quick_reference

        result = quick_reference({}, mock_user)

        # Verify response structure
        assert "content" in result
        assert len(result["content"]) > 0
        content_text = result["content"][0]["text"]
        response_data = json.loads(content_text)

        # Verify existing fields are preserved
        assert "success" in response_data
        assert response_data["success"] is True
        assert "server_identity" in response_data
        assert "TestServer" in response_data["server_identity"]
        assert "total_tools" in response_data
        assert "tools" in response_data

        # Verify langfuse section is added
        assert "langfuse_trace_search" in response_data
