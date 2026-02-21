"""
Unit tests for Story #198: Restart indicators on config settings.

Tests that settings requiring server restart display visual indicators
matching the existing host/port restart indicator pattern.
"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from code_indexer.server.web.routes import RESTART_REQUIRED_FIELDS


def test_restart_indicators_appear_for_worker_settings():
    """Verify restart indicators appear for the 5 worker settings that require server restart."""
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

    # Use the actual production constant from routes.py
    # This ensures the test validates real production behavior, not a local copy
    restart_required_fields = RESTART_REQUIRED_FIELDS

    # Mock context data with all required config sections
    context = {
        "csrf_token": "test-csrf-token",
        "restart_required_fields": restart_required_fields,  # Pass production constant to template
        "config": {
            "server": {
                "host": "127.0.0.1",
                "port": 8000,
                "workers": 4,
                "log_level": "INFO",
                "jwt_expiration_minutes": 10,
                "service_display_name": "Neo",
            },
            "claude_cli": {
                "max_concurrent_claude_cli": 2,
                "description_refresh_interval_hours": 24,
                "research_assistant_timeout_seconds": 300,
                "description_refresh_enabled": False,
                "dependency_map_enabled": False,
                "dependency_map_interval_hours": 168,
                "dependency_map_pass_timeout_seconds": 600,
                "dependency_map_pass1_max_turns": 50,
                "dependency_map_pass2_max_turns": 60,
                "dependency_map_delta_max_turns": 30,
            },
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
            "omni_search": {},
            "content_limits": {},
            "provider_api_keys": {
                "anthropic_configured": False,
                "voyageai_configured": False,
            },
        },
        "validation_errors": {},
        "api_keys_status": [],
        "github_token_data": None,
        "gitlab_token_data": None,
    }

    # Render template
    rendered = template.render(context)

    # AC1 & AC2: Verify restart indicators appear for all 5 new settings
    # The indicator text should match existing host/port pattern: "Requires server restart"

    # Count occurrences of restart indicator
    restart_indicator_count = rendered.count("Requires server restart")

    # We expect exactly 10 total:
    # - 2 server settings: host, port
    # - 2 integration settings: telemetry_enabled, langfuse_enabled
    # - 5 worker settings: max_concurrent_claude_cli, multi_search_max_workers,
    #   scip_multi_max_workers, max_concurrent_background_jobs, subprocess_max_workers
    # - 1 scheduler setting: dependency_map_enabled
    assert (
        restart_indicator_count == 10
    ), f"Expected exactly 10 restart indicators, found {restart_indicator_count}"

    # Verify specific sections contain the indicator near the relevant field labels
    # For max_concurrent_claude_cli
    assert "Max Concurrent Claude CLI" in rendered
    # Check that restart indicator appears in proximity to this field
    # (we'll verify by checking the entire rendered output contains both)

    # For multi_search_max_workers
    assert "Multi-Search Max Workers" in rendered

    # For scip_multi_max_workers
    assert "SCIP Multi Max Workers" in rendered

    # For max_concurrent_background_jobs
    assert "Max Concurrent Background Jobs" in rendered

    # For subprocess_max_workers
    assert "Subprocess Max Workers" in rendered


def test_restart_required_fields_list_contains_expected_fields():
    """Verify the production RESTART_REQUIRED_FIELDS constant contains all expected field names (AC3)."""
    # Validate the actual production constant from routes.py
    # This ensures we're testing real behavior, not a self-referential copy

    # All 10 expected fields
    expected_fields = {
        "host",
        "port",
        "telemetry_enabled",
        "langfuse_enabled",
        "max_concurrent_claude_cli",
        "multi_search_max_workers",
        "scip_multi_max_workers",
        "max_concurrent_background_jobs",
        "subprocess_max_workers",
        "dependency_map_enabled",
    }

    # Validate the production constant
    assert len(RESTART_REQUIRED_FIELDS) == 10, (
        f"Expected 10 restart-required fields, found {len(RESTART_REQUIRED_FIELDS)}"
    )

    # Validate each expected field is present
    for field in expected_fields:
        assert field in RESTART_REQUIRED_FIELDS, (
            f"Expected field '{field}' not found in RESTART_REQUIRED_FIELDS"
        )
