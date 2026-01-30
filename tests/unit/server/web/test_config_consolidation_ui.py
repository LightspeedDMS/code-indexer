"""
Unit tests for Configuration Consolidation Web UI sections (Story #3 - Phase 1).

Tests for the new Web UI sections: Search Limits, File Content Limits, and Golden Repos.
This module uses a shared base class to avoid code duplication across section tests.
"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader


class BaseTemplateSectionTest:
    """Base class for template section tests with shared setup and context."""

    def setup_method(self):
        """Setup Jinja2 environment for template testing."""
        templates_dir = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "web"
            / "templates"
        )
        self.env = Environment(loader=FileSystemLoader(str(templates_dir)))
        self.template = self.env.get_template("partials/config_section.html")

    def _get_base_context(self):
        """Return minimal context for template rendering."""
        return {
            "csrf_token": "test-csrf-token",
            "config": {
                "server": {"host": "127.0.0.1", "port": 8000, "workers": 4, "log_level": "INFO", "jwt_expiration_minutes": 10},
                "cache": {"index_cache_ttl_minutes": 10.0, "fts_cache_ttl_minutes": 10.0, "index_cache_cleanup_interval": 60, "fts_cache_cleanup_interval": 60, "fts_cache_reload_on_access": True, "payload_preview_size_chars": 2000, "payload_max_fetch_size_chars": 5000, "payload_cache_ttl_seconds": 900, "payload_cleanup_interval_seconds": 60},
                "timeouts": {"git_clone_timeout": 3600, "git_pull_timeout": 3600, "git_refresh_timeout": 3600, "cidx_index_timeout": 3600},
                "password_security": {"min_length": 12, "max_length": 128, "required_char_classes": 4, "min_entropy_bits": 50},
                "oidc": {"enabled": False, "scopes": []},
                "job_queue": {"max_total_concurrent_jobs": 4, "max_concurrent_jobs_per_user": 2, "average_job_duration_minutes": 5},
                "telemetry": {"enabled": False, "collector_endpoint": "http://localhost:4317", "collector_protocol": "grpc", "service_name": "cidx-server", "export_traces": True, "export_metrics": True, "export_logs": False, "machine_metrics_enabled": True, "machine_metrics_interval_seconds": 60, "trace_sample_rate": 1.0, "deployment_environment": "development"},
                "claude_delegation": {"is_configured": False, "function_repo_alias": "", "claude_server_url": "", "claude_server_username": "", "claude_server_credential_type": "password", "cidx_callback_url": "", "skip_ssl_verify": False},
                "search_limits": {"max_result_size_mb": 1, "timeout_seconds": 30},
                "file_content_limits": {"max_tokens_per_request": 5000, "chars_per_token": 4},
                "golden_repos": {"refresh_interval_seconds": 3600},
                # Story #3 - Phase 2: P0/P1 settings
                "mcp_session": {"session_ttl_seconds": 3600, "cleanup_interval_seconds": 900},
                "health": {"memory_warning_threshold_percent": 80.0, "memory_critical_threshold_percent": 90.0, "disk_warning_threshold_percent": 80.0, "disk_critical_threshold_percent": 90.0, "cpu_sustained_threshold_percent": 95.0, "system_metrics_cache_ttl_seconds": 5.0},
                "scip": {"indexing_timeout_seconds": 3600, "scip_generation_timeout_seconds": 600, "temporal_stale_threshold_days": 7, "scip_reference_limit": 100, "scip_dependency_depth": 1, "scip_callchain_max_depth": 5, "scip_callchain_limit": 100},
                # Story #3 - Phase 2: P2 settings (AC12-AC26)
                "git_timeouts": {"git_local_timeout": 30, "git_remote_timeout": 300, "github_api_timeout": 30, "gitlab_api_timeout": 30},
                "error_handling": {"max_retry_attempts": 3, "base_retry_delay_seconds": 0.1, "max_retry_delay_seconds": 60.0},
                "api_limits": {"default_file_read_lines": 500, "max_file_read_lines": 5000, "default_diff_lines": 500, "max_diff_lines": 5000, "default_log_commits": 50, "max_log_commits": 500, "audit_log_default_limit": 100, "log_page_size_default": 50, "log_page_size_max": 1000},
                "web_security": {"csrf_max_age_seconds": 600, "web_session_timeout_seconds": 28800},
                # Story #3 - Phase 2: P3 settings (AC36)
                "auth": {"oauth_extension_threshold_hours": 4},
            },
            "validation_errors": {},
            "api_keys_status": [],
            "github_token_data": None,
            "gitlab_token_data": None,
        }


class TestSearchLimitsSectionTemplate(BaseTemplateSectionTest):
    """Test suite for Search Limits section in config_section.html template."""

    def test_search_limits_section_exists(self):
        """Test Search Limits section is rendered in the template."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert "Search Limits" in rendered, "Search Limits section header should be present"
        assert "section-search_limits" in rendered, "Search Limits section ID should be present"

    def test_search_limits_edit_form_exists(self):
        """Test Search Limits edit form has correct inputs."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert 'action="/admin/config/search_limits"' in rendered, "Form should POST to /admin/config/search_limits"
        assert 'name="max_result_size_mb"' in rendered, "max_result_size_mb input should exist"
        assert 'name="timeout_seconds"' in rendered, "timeout_seconds input should exist"

    def test_search_limits_edit_button(self):
        """Test Search Limits section has Edit button."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert "toggleEditMode('search_limits')" in rendered, "Edit button should call toggleEditMode('search_limits')"


class TestFileContentLimitsSectionTemplate(BaseTemplateSectionTest):
    """Test suite for File Content Limits section in config_section.html template."""

    def test_file_content_limits_section_exists(self):
        """Test File Content Limits section is rendered in the template."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert "File Content Limits" in rendered, "File Content Limits section header should be present"
        assert "section-file_content_limits" in rendered, "File Content Limits section ID should be present"

    def test_file_content_limits_edit_form_exists(self):
        """Test File Content Limits edit form has correct inputs."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert 'action="/admin/config/file_content_limits"' in rendered, "Form should POST to /admin/config/file_content_limits"
        assert 'name="max_tokens_per_request"' in rendered, "max_tokens_per_request input should exist"
        assert 'name="chars_per_token"' in rendered, "chars_per_token input should exist"

    def test_file_content_limits_edit_button(self):
        """Test File Content Limits section has Edit button."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert "toggleEditMode('file_content_limits')" in rendered, "Edit button should call toggleEditMode('file_content_limits')"


class TestGoldenReposSectionTemplate(BaseTemplateSectionTest):
    """Test suite for Golden Repos section in config_section.html template."""

    def test_golden_repos_section_exists(self):
        """Test Golden Repos section is rendered in the template."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert "Golden Repos" in rendered, "Golden Repos section header should be present"
        assert "section-golden_repos" in rendered, "Golden Repos section ID should be present"

    def test_golden_repos_edit_form_exists(self):
        """Test Golden Repos edit form has correct inputs."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert 'action="/admin/config/golden_repos"' in rendered, "Form should POST to /admin/config/golden_repos"
        assert 'name="refresh_interval_seconds"' in rendered, "refresh_interval_seconds input should exist"

    def test_golden_repos_edit_button(self):
        """Test Golden Repos section has Edit button."""
        context = self._get_base_context()
        rendered = self.template.render(context)

        assert "toggleEditMode('golden_repos')" in rendered, "Edit button should call toggleEditMode('golden_repos')"
