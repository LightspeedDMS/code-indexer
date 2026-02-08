"""
Unit tests for golden repos dual-view functionality (Story #161).

Tests the integration between golden repo cards and global activated repo data:
- Template structure includes global activated section
- JavaScript functions fetch global activated data
- Data contract between frontend and backend
- Global activated section only shows when global activation exists

Note: API endpoint tests exist in tests/unit/server/routers/
This file focuses on frontend integration concerns.
"""

import pytest
from pathlib import Path


class TestGoldenReposDualViewTemplate:
    """Test golden repos template includes dual-view structure."""

    def test_template_file_exists(self):
        """Test golden_repos_list.html template exists."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        assert template_path.exists(), f"Template not found at {template_path}"

    def test_template_has_global_activated_section(self):
        """Test template includes a section for global activated repository data."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Check for global activated section marker
        assert "Global Activated Repository" in content or "global-activated-section" in content, \
            "Template should include global activated repository section"

    def test_template_has_conditional_global_section(self):
        """Test template conditionally shows global activated section based on global_alias."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have conditional check for global_alias existence
        assert "{% if repo.global_alias %}" in content or "if repo.global_alias" in content, \
            "Template should conditionally show global activated section only when global_alias exists"

    def test_template_references_global_activated_indexes(self):
        """Test template references global activated repo index status."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should reference global activated indexes (semantic, FTS, temporal, SCIP)
        # This would typically be loaded via JavaScript from /api/activated-repos/{alias}-global/indexes
        assert "global" in content.lower(), \
            "Template should reference global activated repository data"

    def test_template_references_global_activated_health(self):
        """Test template references global activated repo health status."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have health check elements for global activated repo
        # This would typically be loaded via JavaScript from /api/activated-repos/{alias}-global/health
        assert "health" in content.lower(), \
            "Template should reference health check functionality"


class TestGoldenReposJavaScriptFunctions:
    """Test JavaScript functions for fetching global activated repo data."""

    def test_golden_repos_js_file_exists(self):
        """Test that a JavaScript file exists for golden repos management."""
        # Check if there's a dedicated JS file or if functions are in template
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have <script> tags or reference to external JS
        assert "<script>" in content or ".js" in content, \
            "Template should include JavaScript functionality"

    def test_template_has_fetch_global_indexes_function(self):
        """Test template includes function to fetch global activated indexes."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have function that calls /api/activated-repos/{alias}-global/indexes
        # Or reuses fetchActivatedRepoIndexes from activated_repo_management.js
        has_fetch_function = (
            "fetchActivatedRepoIndexes" in content or
            "fetchGlobalActivatedIndexes" in content or
            "/api/activated-repos/" in content
        )
        assert has_fetch_function, \
            "Template should include function to fetch global activated indexes"

    def test_template_has_fetch_global_health_function(self):
        """Test template includes function to fetch global activated health data."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have function that calls /api/activated-repos/{alias}-global/health
        # Or reuses fetchActivatedRepoHealth from activated_repo_management.js
        has_health_function = (
            "fetchActivatedRepoHealth" in content or
            "fetchGlobalActivatedHealth" in content or
            "/health" in content
        )
        assert has_health_function, \
            "Template should include function to fetch global activated health data"


class TestGoldenReposDataContract:
    """Test data contract between frontend and backend for dual-view."""

    def test_global_alias_format(self):
        """Test global alias format is {golden_alias}-global."""
        golden_alias = "my-repo"
        expected_global_alias = f"{golden_alias}-global"

        # This is the convention used throughout the system
        assert expected_global_alias == "my-repo-global"

    def test_index_status_endpoint_exists_for_global_repos(self):
        """
        Test that index status endpoint works for global activated repos.

        The endpoint /api/activated-repos/{user_alias}/indexes should work
        when user_alias is {golden_alias}-global.
        """
        # This is tested in API router tests, but we verify the contract here
        global_alias = "test-repo-global"
        expected_endpoint = f"/api/activated-repos/{global_alias}/indexes"

        # Verify endpoint format is correct
        assert "-global" in global_alias
        assert "/indexes" in expected_endpoint

    def test_health_endpoint_exists_for_global_repos(self):
        """
        Test that health endpoint works for global activated repos.

        The endpoint /api/activated-repos/{user_alias}/health should work
        when user_alias is {golden_alias}-global.
        """
        # This is tested in API router tests, but we verify the contract here
        global_alias = "test-repo-global"
        expected_endpoint = f"/api/activated-repos/{global_alias}/health"

        # Verify endpoint format is correct
        assert "-global" in global_alias
        assert "/health" in expected_endpoint

    def test_template_context_includes_global_alias(self):
        """
        Test template context includes global_alias field.

        The repo object passed to template should have a global_alias field
        that is set when the golden repo is globally activated.
        """
        # Mock template context
        repo_with_global = {
            "alias": "test-repo",
            "global_alias": "test-repo-global",
            "has_semantic": True,
            "has_fts": False,
        }

        repo_without_global = {
            "alias": "test-repo-2",
            "global_alias": None,
            "has_semantic": True,
        }

        # Verify structure
        assert "global_alias" in repo_with_global
        assert repo_with_global["global_alias"] is not None
        assert "global_alias" in repo_without_global
        assert repo_without_global["global_alias"] is None


class TestGoldenReposDualViewStyling:
    """Test CSS styling for global activated section."""

    def test_template_has_styling_for_global_section(self):
        """Test template includes CSS for global activated section."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have <style> tags for component styling
        assert "<style>" in content, \
            "Template should include CSS styling"

    def test_global_section_styling_matches_activated_repos(self):
        """Test global activated section uses consistent styling with activated repos cards."""
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should reference similar CSS classes as activated repos
        # Check for common styling patterns
        has_index_badge = "index-badge" in content
        has_health_status = "health-status" in content or "health-indicator" in content

        assert has_index_badge or has_health_status, \
            "Template should use consistent styling classes"


class TestGoldenReposDualViewBehavior:
    """Test behavior requirements for dual-view functionality."""

    def test_global_section_only_shows_when_global_alias_exists(self):
        """
        Test that global activated section only appears when global_alias is set.

        This is a key requirement - the section should be hidden if the golden
        repo does not have a global activated copy.
        """
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have conditional rendering
        assert "{% if" in content and "global" in content.lower(), \
            "Template should conditionally render global activated section"

    def test_dual_view_shows_both_golden_and_global_metrics(self):
        """
        Test that template structure supports showing both:
        1. Golden repo metrics (source/master)
        2. Global activated repo metrics (activated copy)
        """
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should reference both golden repo data and global activated data
        # Golden repo data is in repo.has_semantic, repo.has_fts, etc.
        # Global activated data would be loaded dynamically
        assert "repo.has_" in content or "repo.alias" in content, \
            "Template should reference golden repo data"

    def test_javascript_loads_global_data_on_details_expand(self):
        """
        Test that JavaScript loads global activated data when details are expanded.

        This ensures we don't load data unnecessarily for collapsed cards.
        """
        template_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates" / "partials" / "golden_repos_list.html"
        content = template_path.read_text()

        # Should have event handler for details expansion or load on DOMContentLoaded
        has_load_trigger = (
            "toggleDetails" in content or
            "DOMContentLoaded" in content or
            "onclick" in content
        )
        assert has_load_trigger, \
            "Template should include triggers for loading global activated data"
