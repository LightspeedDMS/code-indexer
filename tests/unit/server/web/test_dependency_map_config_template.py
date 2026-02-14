"""
Unit tests for dependency map configuration settings in config_section.html template.

Tests that the template renders all 7 dependency map settings correctly in both
display and edit modes, and that the restart indicator appears for dependency_map_enabled.
"""

import pytest
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS


@pytest.fixture
def templates_env():
    """Setup Jinja2 environment for template tests."""
    templates_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
    )
    return Environment(loader=FileSystemLoader(str(templates_dir)))


@pytest.fixture
def base_config_context():
    """Return base context dict for config template tests."""
    return {
        "csrf_token": "test-csrf-token",
        "restart_required_fields": RESTART_REQUIRED_FIELDS,
        "config": {
            "server": {"host": "127.0.0.1", "port": 8000, "workers": 4, "log_level": "INFO", "jwt_expiration_minutes": 10},
            "cache": {"index_cache_ttl_minutes": 10.0, "fts_cache_ttl_minutes": 10.0, "index_cache_cleanup_interval": 60,
                      "fts_cache_cleanup_interval": 60, "fts_cache_reload_on_access": True, "payload_preview_size_chars": 2000,
                      "payload_max_fetch_size_chars": 5000, "payload_cache_ttl_seconds": 900, "payload_cleanup_interval_seconds": 60},
            "timeouts": {"git_clone_timeout": 3600, "git_pull_timeout": 3600, "git_refresh_timeout": 3600, "cidx_index_timeout": 3600},
            "password_security": {"min_length": 12, "max_length": 128, "required_char_classes": 4, "min_entropy_bits": 50},
            "oidc": {"enabled": False, "provider_name": "SSO", "scopes": []},
            "job_queue": {"max_total_concurrent_jobs": 4, "max_concurrent_jobs_per_user": 2, "average_job_duration_minutes": 5},
            "telemetry": {"enabled": False, "collector_endpoint": "http://localhost:4317", "collector_protocol": "grpc",
                          "service_name": "cidx-server", "export_traces": True, "export_metrics": True, "export_logs": False,
                          "machine_metrics_enabled": True, "machine_metrics_interval_seconds": 60, "trace_sample_rate": 1.0,
                          "deployment_environment": "development"},
            "langfuse": {"enabled": False, "public_key": "", "secret_key": "", "host": ""},
            "claude_delegation": {"is_configured": False, "function_repo_alias": "", "claude_server_url": "",
                                  "claude_server_username": "", "claude_server_credential_type": "password",
                                  "cidx_callback_url": "", "skip_ssl_verify": False},
            "search_limits": {"max_result_size_mb": 1, "timeout_seconds": 30},
            "file_content_limits": {"max_tokens_per_request": 5000, "chars_per_token": 4},
            "golden_repos": {"refresh_interval_seconds": 3600},
            "mcp_session": {"session_ttl_seconds": 3600, "cleanup_interval_seconds": 900},
            "health": {"memory_warning_threshold_percent": 80.0, "memory_critical_threshold_percent": 90.0,
                       "disk_warning_threshold_percent": 80.0, "disk_critical_threshold_percent": 90.0,
                       "cpu_sustained_threshold_percent": 95.0, "system_metrics_cache_ttl_seconds": 5.0},
            "scip": {"indexing_timeout_seconds": 3600, "scip_generation_timeout_seconds": 600,
                     "temporal_stale_threshold_days": 7, "scip_reference_limit": 100, "scip_dependency_depth": 1,
                     "scip_callchain_max_depth": 5, "scip_callchain_limit": 100},
            "git_timeouts": {"git_local_timeout": 30, "git_remote_timeout": 300, "github_api_timeout": 30, "gitlab_api_timeout": 30},
            "error_handling": {"max_retry_attempts": 3, "base_retry_delay_seconds": 0.1, "max_retry_delay_seconds": 60.0},
            "api_limits": {"default_file_read_lines": 500, "max_file_read_lines": 5000, "default_diff_lines": 500,
                           "max_diff_lines": 5000, "default_log_commits": 50, "max_log_commits": 500,
                           "audit_log_default_limit": 100, "log_page_size_default": 50, "log_page_size_max": 1000},
            "web_security": {"csrf_max_age_seconds": 600, "web_session_timeout_seconds": 28800},
            "auth": {"oauth_extension_threshold_hours": 4},
            "multi_search": {"multi_search_max_workers": 2, "multi_search_timeout_seconds": 30,
                             "scip_multi_max_workers": 2, "scip_multi_timeout_seconds": 30},
            "background_jobs": {"max_concurrent_background_jobs": 5, "subprocess_max_workers": 2},
            "claude_cli": {
                "max_concurrent_claude_cli": 2,
                "description_refresh_interval_hours": 24,
                "description_refresh_enabled": False,
                "research_assistant_timeout_seconds": 300,
                "dependency_map_enabled": False,
                "dependency_map_interval_hours": 168,
                "dependency_map_pass_timeout_seconds": 600,
                "dependency_map_pass1_max_turns": 50,
                "dependency_map_pass2_max_turns": 60,
                "dependency_map_pass3_max_turns": 30,
                "dependency_map_delta_max_turns": 30,
            },
            "provider_api_keys": {"anthropic_configured": False, "voyageai_configured": False},
            "omni_search": {},
            "content_limits": {},
        },
        "validation_errors": {},
        "api_keys_status": [],
        "github_token_data": None,
        "gitlab_token_data": None,
    }


def test_dependency_map_settings_render_in_display_mode(templates_env, base_config_context):
    """Verify all 7 dependency map settings appear in display mode with correct default values."""
    template = templates_env.get_template("partials/config_section.html")
    base_config_context["config"]["claude_cli"]["dependency_map_enabled"] = False

    rendered = template.render(base_config_context)

    # Verify Dependency Map subsection header appears
    assert "Dependency Map" in rendered, "Dependency Map subsection header should appear"

    # Verify all 7 dependency map fields appear with correct values
    assert "Dependency Map Enabled" in rendered
    assert "No" in rendered  # dependency_map_enabled=False displays as "No"
    assert "Refresh Interval (hours)" in rendered
    assert "168" in rendered  # default interval
    assert "Pass Timeout (seconds)" in rendered
    assert "600" in rendered  # default timeout
    assert "Pass 1 Max Turns (Synthesis)" in rendered
    assert "50" in rendered  # pass1 default
    assert "Pass 2 Max Turns (Per-Domain)" in rendered
    assert "60" in rendered  # pass2 default
    assert "Pass 3 Max Turns (Index)" in rendered
    assert "30" in rendered  # pass3 and delta default (both use 30)
    assert "Delta Max Turns" in rendered


def test_dependency_map_settings_render_in_edit_mode(templates_env, base_config_context):
    """Verify all 7 dependency map form fields appear in edit mode with correct input types."""
    template = templates_env.get_template("partials/config_section.html")
    base_config_context["config"]["claude_cli"]["dependency_map_enabled"] = True

    rendered = template.render(base_config_context)

    # Verify form fields exist with correct names and IDs
    assert 'id="depmap-enabled"' in rendered
    assert 'name="dependency_map_enabled"' in rendered
    assert '<select id="depmap-enabled"' in rendered
    assert 'id="depmap-interval"' in rendered
    assert 'name="dependency_map_interval_hours"' in rendered
    assert 'type="number"' in rendered
    assert 'id="depmap-pass-timeout"' in rendered
    assert 'name="dependency_map_pass_timeout_seconds"' in rendered
    assert 'id="depmap-pass1-turns"' in rendered
    assert 'name="dependency_map_pass1_max_turns"' in rendered
    assert 'id="depmap-pass2-turns"' in rendered
    assert 'name="dependency_map_pass2_max_turns"' in rendered
    assert 'id="depmap-pass3-turns"' in rendered
    assert 'name="dependency_map_pass3_max_turns"' in rendered
    assert 'id="depmap-delta-turns"' in rendered
    assert 'name="dependency_map_delta_max_turns"' in rendered

    # Verify default values are rendered
    assert 'value="168"' in rendered
    assert 'value="600"' in rendered
    assert 'value="50"' in rendered
    assert 'value="60"' in rendered
    assert 'value="30"' in rendered


def test_dependency_map_enabled_has_restart_indicator(templates_env, base_config_context):
    """Verify dependency_map_enabled field shows restart indicator."""
    # Verify dependency_map_enabled is in RESTART_REQUIRED_FIELDS
    assert (
        "dependency_map_enabled" in RESTART_REQUIRED_FIELDS
    ), "dependency_map_enabled must be in RESTART_REQUIRED_FIELDS"

    template = templates_env.get_template("partials/config_section.html")
    base_config_context["config"]["claude_cli"]["dependency_map_enabled"] = False

    rendered = template.render(base_config_context)

    # Count restart indicators - should now be 10 (9 existing + 1 for dependency_map_enabled)
    restart_indicator_count = rendered.count("Requires server restart")
    assert (
        restart_indicator_count == 10
    ), f"Expected 10 restart indicators after adding dependency_map_enabled, found {restart_indicator_count}"
