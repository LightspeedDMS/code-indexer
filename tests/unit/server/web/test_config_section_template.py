"""
Unit tests for config_section.html template rendering.

Tests that the template renders correctly with various data contexts,
particularly focusing on API Keys section that was having Jinja2 syntax errors.
"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS


def test_api_keys_section_renders_with_github_token():
    """Verify API Keys section renders when GitHub token is configured."""
    # Setup Jinja2 environment
    templates_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
    )
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    template = env.get_template("partials/config_section.html")

    # Mock context data
    context = {
        "csrf_token": "test-csrf-token",
        "restart_required_fields": RESTART_REQUIRED_FIELDS,
        "config": {
            "server": {
                "host": "127.0.0.1",
                "port": 8000,
                "workers": 4,
                "log_level": "INFO",
                "jwt_expiration_minutes": 10,
                "service_display_name": "Neo",
            },
            "cache": {
                "index_cache_ttl_minutes": 10.0,
                "fts_cache_ttl_minutes": 10.0,
                "index_cache_cleanup_interval": 60,
                "fts_cache_cleanup_interval": 60,
                "fts_cache_reload_on_access": True,
                "payload_preview_size_chars": 2000,
                "payload_max_fetch_size_chars": 5000,
                "payload_cache_ttl_seconds": 900,
                "payload_cleanup_interval_seconds": 60,
            },
            "timeouts": {
                "git_clone_timeout": 3600,
                "git_pull_timeout": 3600,
                "git_refresh_timeout": 3600,
                "cidx_index_timeout": 3600,
            },
            "password_security": {
                "min_length": 12,
                "max_length": 128,
                "required_char_classes": 4,
                "min_entropy_bits": 50,
            },
            "oidc": {"enabled": False, "provider_name": "SSO", "scopes": []},
            "job_queue": {
                "max_total_concurrent_jobs": 4,
                "max_concurrent_jobs_per_user": 2,
                "average_job_duration_minutes": 5,
            },
            "telemetry": {
                "enabled": False,
                "collector_endpoint": "http://localhost:4317",
                "collector_protocol": "grpc",
                "service_name": "cidx-server",
                "export_traces": True,
                "export_metrics": True,
                "export_logs": False,
                "machine_metrics_enabled": True,
                "machine_metrics_interval_seconds": 60,
                "trace_sample_rate": 1.0,
                "deployment_environment": "development",
            },
            "langfuse": {
                "enabled": False,
                "public_key": "",
                "secret_key": "",
                "host": "",
            },
            "claude_delegation": {
                "is_configured": False,
                "function_repo_alias": "",
                "claude_server_url": "",
                "claude_server_username": "",
                "claude_server_credential_type": "password",
                "cidx_callback_url": "",
                "skip_ssl_verify": False,
            },
            "search_limits": {"max_result_size_mb": 1, "timeout_seconds": 30},
            "file_content_limits": {
                "max_tokens_per_request": 5000,
                "chars_per_token": 4,
            },
            "golden_repos": {"refresh_interval_seconds": 3600},
            "mcp_session": {
                "session_ttl_seconds": 3600,
                "cleanup_interval_seconds": 900,
            },
            "health": {
                "memory_warning_threshold_percent": 80.0,
                "memory_critical_threshold_percent": 90.0,
                "disk_warning_threshold_percent": 80.0,
                "disk_critical_threshold_percent": 90.0,
                "cpu_sustained_threshold_percent": 95.0,
                "system_metrics_cache_ttl_seconds": 5.0,
            },
            "scip": {
                "indexing_timeout_seconds": 3600,
                "scip_generation_timeout_seconds": 600,
                "temporal_stale_threshold_days": 7,
                "scip_reference_limit": 100,
                "scip_dependency_depth": 1,
                "scip_callchain_max_depth": 5,
                "scip_callchain_limit": 100,
            },
            "git_timeouts": {
                "git_local_timeout": 30,
                "git_remote_timeout": 300,
                "github_api_timeout": 30,
                "gitlab_api_timeout": 30,
            },
            "error_handling": {
                "max_retry_attempts": 3,
                "base_retry_delay_seconds": 0.1,
                "max_retry_delay_seconds": 60.0,
            },
            "api_limits": {
                "default_file_read_lines": 500,
                "max_file_read_lines": 5000,
                "default_diff_lines": 500,
                "max_diff_lines": 5000,
                "default_log_commits": 50,
                "max_log_commits": 500,
                "audit_log_default_limit": 100,
                "log_page_size_default": 50,
                "log_page_size_max": 1000,
            },
            "web_security": {
                "csrf_max_age_seconds": 600,
                "web_session_timeout_seconds": 28800,
            },
            "auth": {"oauth_extension_threshold_hours": 4},
            "multi_search": {
                "multi_search_max_workers": 2,
                "multi_search_timeout_seconds": 30,
                "scip_multi_max_workers": 2,
                "scip_multi_timeout_seconds": 30,
            },
            "background_jobs": {
                "max_concurrent_background_jobs": 5,
                "subprocess_max_workers": 2,
            },
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
            "provider_api_keys": {
                "anthropic_configured": False,
                "voyageai_configured": False,
            },
            "omni_search": {},
            "content_limits": {},
        },
        "validation_errors": {},
        "api_keys_status": [],
        "github_token_data": {
            "token": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
            "platform": "github",
        },
        "gitlab_token_data": None,
    }

    # Render template
    rendered = template.render(context)

    # Verify API Keys section is present
    assert (
        "CI/CD Platform Keys" in rendered
    ), "API Keys section header should be in rendered HTML"
    assert "GitHub" in rendered, "GitHub subsection should be in rendered HTML"
    assert "GitLab" in rendered, "GitLab subsection should be in rendered HTML"

    # Verify token is masked in DISPLAY mode (shows prefix and asterisks)
    assert "ghp_" in rendered, "Token prefix should be visible"
    assert (
        "**************************" in rendered
    ), "Token should be masked with asterisks in display mode"

    # Note: Full token WILL appear in edit form's password input value attribute
    # This is expected - it's in a password field (browser-masked) for editing
    # The security property we care about: masked in the visible display table


def test_api_keys_section_renders_without_tokens():
    """Verify API Keys section renders when no tokens configured."""
    # Setup Jinja2 environment
    templates_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
    )
    env = Environment(loader=FileSystemLoader(str(templates_dir)))
    template = env.get_template("partials/config_section.html")

    # Mock context data with no tokens
    context = {
        "csrf_token": "test-csrf-token",
        "restart_required_fields": RESTART_REQUIRED_FIELDS,
        "config": {
            "server": {
                "host": "127.0.0.1",
                "port": 8000,
                "workers": 4,
                "log_level": "INFO",
                "jwt_expiration_minutes": 10,
                "service_display_name": "Neo",
            },
            "cache": {
                "index_cache_ttl_minutes": 10.0,
                "fts_cache_ttl_minutes": 10.0,
                "index_cache_cleanup_interval": 60,
                "fts_cache_cleanup_interval": 60,
                "fts_cache_reload_on_access": True,
                "payload_preview_size_chars": 2000,
                "payload_max_fetch_size_chars": 5000,
                "payload_cache_ttl_seconds": 900,
                "payload_cleanup_interval_seconds": 60,
            },
            "timeouts": {
                "git_clone_timeout": 3600,
                "git_pull_timeout": 3600,
                "git_refresh_timeout": 3600,
                "cidx_index_timeout": 3600,
            },
            "password_security": {
                "min_length": 12,
                "max_length": 128,
                "required_char_classes": 4,
                "min_entropy_bits": 50,
            },
            "oidc": {"enabled": False, "provider_name": "SSO", "scopes": []},
            "job_queue": {
                "max_total_concurrent_jobs": 4,
                "max_concurrent_jobs_per_user": 2,
                "average_job_duration_minutes": 5,
            },
            "telemetry": {
                "enabled": False,
                "collector_endpoint": "http://localhost:4317",
                "collector_protocol": "grpc",
                "service_name": "cidx-server",
                "export_traces": True,
                "export_metrics": True,
                "export_logs": False,
                "machine_metrics_enabled": True,
                "machine_metrics_interval_seconds": 60,
                "trace_sample_rate": 1.0,
                "deployment_environment": "development",
            },
            "langfuse": {
                "enabled": False,
                "public_key": "",
                "secret_key": "",
                "host": "",
            },
            "claude_delegation": {
                "is_configured": False,
                "function_repo_alias": "",
                "claude_server_url": "",
                "claude_server_username": "",
                "claude_server_credential_type": "password",
                "cidx_callback_url": "",
                "skip_ssl_verify": False,
            },
            "search_limits": {"max_result_size_mb": 1, "timeout_seconds": 30},
            "file_content_limits": {
                "max_tokens_per_request": 5000,
                "chars_per_token": 4,
            },
            "golden_repos": {"refresh_interval_seconds": 3600},
            "mcp_session": {
                "session_ttl_seconds": 3600,
                "cleanup_interval_seconds": 900,
            },
            "health": {
                "memory_warning_threshold_percent": 80.0,
                "memory_critical_threshold_percent": 90.0,
                "disk_warning_threshold_percent": 80.0,
                "disk_critical_threshold_percent": 90.0,
                "cpu_sustained_threshold_percent": 95.0,
                "system_metrics_cache_ttl_seconds": 5.0,
            },
            "scip": {
                "indexing_timeout_seconds": 3600,
                "scip_generation_timeout_seconds": 600,
                "temporal_stale_threshold_days": 7,
                "scip_reference_limit": 100,
                "scip_dependency_depth": 1,
                "scip_callchain_max_depth": 5,
                "scip_callchain_limit": 100,
            },
            "git_timeouts": {
                "git_local_timeout": 30,
                "git_remote_timeout": 300,
                "github_api_timeout": 30,
                "gitlab_api_timeout": 30,
            },
            "error_handling": {
                "max_retry_attempts": 3,
                "base_retry_delay_seconds": 0.1,
                "max_retry_delay_seconds": 60.0,
            },
            "api_limits": {
                "default_file_read_lines": 500,
                "max_file_read_lines": 5000,
                "default_diff_lines": 500,
                "max_diff_lines": 5000,
                "default_log_commits": 50,
                "max_log_commits": 500,
                "audit_log_default_limit": 100,
                "log_page_size_default": 50,
                "log_page_size_max": 1000,
            },
            "web_security": {
                "csrf_max_age_seconds": 600,
                "web_session_timeout_seconds": 28800,
            },
            "auth": {"oauth_extension_threshold_hours": 4},
            "multi_search": {
                "multi_search_max_workers": 2,
                "multi_search_timeout_seconds": 30,
                "scip_multi_max_workers": 2,
                "scip_multi_timeout_seconds": 30,
            },
            "background_jobs": {
                "max_concurrent_background_jobs": 5,
                "subprocess_max_workers": 2,
            },
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
            "provider_api_keys": {
                "anthropic_configured": False,
                "voyageai_configured": False,
            },
            "omni_search": {},
            "content_limits": {},
        },
        "validation_errors": {},
        "api_keys_status": [],
        "github_token_data": None,
        "gitlab_token_data": None,
    }

    # Render template
    rendered = template.render(context)

    # Verify API Keys section is present
    assert (
        "CI/CD Platform Keys" in rendered
    ), "API Keys section header should be in rendered HTML"
    assert "GitHub" in rendered, "GitHub subsection should be in rendered HTML"
    assert "GitLab" in rendered, "GitLab subsection should be in rendered HTML"

    # Verify "Not configured" state is shown
    assert (
        "Configure" in rendered
    ), "Configure button should be shown for unconfigured tokens"
