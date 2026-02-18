"""
Unit tests for Story #222 Phase 2 TODOs 4-6: CI/CD tool consolidation.

  TODO 4: Delete 6 gh_actions_*.md tool doc files.
  TODO 5: Remove 6 HANDLER_REGISTRY entries (keep handler functions for REST).
  TODO 6: Update tool count assertion.

TDD: These tests are written FIRST to define expected behavior.
"""

from pathlib import Path


TOOL_DOCS_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
)

GH_ACTIONS_TOOLS = [
    "gh_actions_list_runs",
    "gh_actions_get_run",
    "gh_actions_search_logs",
    "gh_actions_get_job_logs",
    "gh_actions_retry_run",
    "gh_actions_cancel_run",
]


class TestGhActionsToolDocFilesDeleted:
    """The 6 gh_actions_*.md files must be deleted from tool_docs/cicd/."""

    def test_gh_actions_list_runs_md_deleted(self):
        """gh_actions_list_runs.md must not exist in tool_docs/cicd/."""
        md_file = TOOL_DOCS_DIR / "cicd" / "gh_actions_list_runs.md"
        assert not md_file.exists(), f"File must be deleted: {md_file}"

    def test_gh_actions_get_run_md_deleted(self):
        """gh_actions_get_run.md must not exist in tool_docs/cicd/."""
        md_file = TOOL_DOCS_DIR / "cicd" / "gh_actions_get_run.md"
        assert not md_file.exists(), f"File must be deleted: {md_file}"

    def test_gh_actions_search_logs_md_deleted(self):
        """gh_actions_search_logs.md must not exist in tool_docs/cicd/."""
        md_file = TOOL_DOCS_DIR / "cicd" / "gh_actions_search_logs.md"
        assert not md_file.exists(), f"File must be deleted: {md_file}"

    def test_gh_actions_get_job_logs_md_deleted(self):
        """gh_actions_get_job_logs.md must not exist in tool_docs/cicd/."""
        md_file = TOOL_DOCS_DIR / "cicd" / "gh_actions_get_job_logs.md"
        assert not md_file.exists(), f"File must be deleted: {md_file}"

    def test_gh_actions_retry_run_md_deleted(self):
        """gh_actions_retry_run.md must not exist in tool_docs/cicd/."""
        md_file = TOOL_DOCS_DIR / "cicd" / "gh_actions_retry_run.md"
        assert not md_file.exists(), f"File must be deleted: {md_file}"

    def test_gh_actions_cancel_run_md_deleted(self):
        """gh_actions_cancel_run.md must not exist in tool_docs/cicd/."""
        md_file = TOOL_DOCS_DIR / "cicd" / "gh_actions_cancel_run.md"
        assert not md_file.exists(), f"File must be deleted: {md_file}"


class TestGhActionsRemovedFromRegistry:
    """The 6 gh_actions_* tools must not appear in TOOL_REGISTRY or HANDLER_REGISTRY."""

    def test_gh_actions_list_runs_not_in_tool_registry(self):
        """gh_actions_list_runs must not be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        assert "gh_actions_list_runs" not in TOOL_REGISTRY

    def test_gh_actions_get_run_not_in_tool_registry(self):
        """gh_actions_get_run must not be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        assert "gh_actions_get_run" not in TOOL_REGISTRY

    def test_gh_actions_search_logs_not_in_tool_registry(self):
        """gh_actions_search_logs must not be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        assert "gh_actions_search_logs" not in TOOL_REGISTRY

    def test_gh_actions_get_job_logs_not_in_tool_registry(self):
        """gh_actions_get_job_logs must not be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        assert "gh_actions_get_job_logs" not in TOOL_REGISTRY

    def test_gh_actions_retry_run_not_in_tool_registry(self):
        """gh_actions_retry_run must not be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        assert "gh_actions_retry_run" not in TOOL_REGISTRY

    def test_gh_actions_cancel_run_not_in_tool_registry(self):
        """gh_actions_cancel_run must not be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        assert "gh_actions_cancel_run" not in TOOL_REGISTRY

    def test_gh_actions_not_in_handler_registry(self):
        """All 6 gh_actions_* tools must not be in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        for tool_name in GH_ACTIONS_TOOLS:
            assert tool_name not in HANDLER_REGISTRY, (
                f"{tool_name} still in HANDLER_REGISTRY - "
                "remove the HANDLER_REGISTRY entry (keep the function for REST)"
            )


class TestGhActionsHandlerFunctionsPreserved:
    """Handler functions must still exist because REST routes in cicd.py depend on them."""

    def test_handle_gh_actions_list_runs_function_exists(self):
        """handle_gh_actions_list_runs function must still exist for REST routes."""
        import code_indexer.server.mcp.handlers as handlers_module
        assert hasattr(handlers_module, "handle_gh_actions_list_runs"), (
            "handle_gh_actions_list_runs was removed - REST routes in cicd.py need it"
        )

    def test_handle_gh_actions_get_run_function_exists(self):
        """handle_gh_actions_get_run function must still exist for REST routes."""
        import code_indexer.server.mcp.handlers as handlers_module
        assert hasattr(handlers_module, "handle_gh_actions_get_run"), (
            "handle_gh_actions_get_run was removed - REST routes in cicd.py need it"
        )

    def test_handle_gh_actions_search_logs_function_exists(self):
        """handle_gh_actions_search_logs function must still exist for REST routes."""
        import code_indexer.server.mcp.handlers as handlers_module
        assert hasattr(handlers_module, "handle_gh_actions_search_logs"), (
            "handle_gh_actions_search_logs was removed - REST routes in cicd.py need it"
        )

    def test_handle_gh_actions_get_job_logs_function_exists(self):
        """handle_gh_actions_get_job_logs function must still exist for REST routes."""
        import code_indexer.server.mcp.handlers as handlers_module
        assert hasattr(handlers_module, "handle_gh_actions_get_job_logs"), (
            "handle_gh_actions_get_job_logs was removed - REST routes in cicd.py need it"
        )

    def test_handle_gh_actions_retry_run_function_exists(self):
        """handle_gh_actions_retry_run function must still exist for REST routes."""
        import code_indexer.server.mcp.handlers as handlers_module
        assert hasattr(handlers_module, "handle_gh_actions_retry_run"), (
            "handle_gh_actions_retry_run was removed - REST routes in cicd.py need it"
        )

    def test_handle_gh_actions_cancel_run_function_exists(self):
        """handle_gh_actions_cancel_run function must still exist for REST routes."""
        import code_indexer.server.mcp.handlers as handlers_module
        assert hasattr(handlers_module, "handle_gh_actions_cancel_run"), (
            "handle_gh_actions_cancel_run was removed - REST routes in cicd.py need it"
        )


class TestToolRegistryCountAfterRemoval:
    """TOOL_REGISTRY must have 127 tools after removing 6 gh_actions tools."""

    def test_registry_contains_127_tools(self):
        """TOOL_REGISTRY must have exactly 127 tools (133 - 6 gh_actions removed)."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        assert len(TOOL_REGISTRY) == 127, (
            f"Expected 127 tools after removing 6 gh_actions, got {len(TOOL_REGISTRY)}"
        )
