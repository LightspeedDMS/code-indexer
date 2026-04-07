"""MCP handler package — backward-compatible namespace.

Phase 2+: This package imports all symbols from _legacy.py (and eventually
from individual domain modules) into its own namespace so that:

  - patch("code_indexer.server.mcp.handlers.X") patches the binding in this
    __init__.py, which is what external code sees — preserving all mock patches.
  - Domain submodules (search.py, repos.py, ...) can be imported as
    code_indexer.server.mcp.handlers.search etc.

As each domain module is extracted from _legacy.py, the corresponding
'from ._legacy import X' line below is replaced with 'from .domain import X'.
"""

# Re-export everything from _legacy so the package namespace is complete.
# Explicit star-import makes this a real package (not a sys.modules alias)
# which allows submodule imports (handlers.search, handlers.repos, etc.).
# Note: star-import does NOT re-export private names (starting with _), so
# we must list them explicitly below.
from code_indexer.server.mcp.handlers._legacy import *  # noqa: F401, F403

# Explicitly re-export private names used by tests and external consumers.
# Star-import skips names beginning with '_', so they must be listed here.
# Sources: _utils.py (shared utilities) and _legacy.py (domain-specific helpers).
from code_indexer.server.mcp.handlers._legacy import (  # noqa: F401
    # Package-level attributes expected by protocol.py and tests
    app_module,
    HANDLER_REGISTRY,
    # Utilities (originally in _legacy, now imported there from _utils)
    _apply_fts_payload_truncation,
    _apply_payload_truncation,
    _apply_regex_payload_truncation,
    _apply_scip_payload_truncation,
    _apply_temporal_payload_truncation,
    _coerce_float,
    _coerce_int,
    _enrich_with_wiki_url,
    _error_with_suggestions,
    _expand_wildcard_patterns,
    _format_omni_response,
    _get_access_filtering_service,
    _get_app_refresh_scheduler,
    _get_available_repos,
    _get_global_repo,
    _get_golden_repos_dir,
    _get_hnsw_health_service,
    _get_query_tracker,
    _get_scip_audit_repository,
    _get_scip_query_service,
    _get_temporal_status,
    _get_wiki_enabled_repos,
    _has_wildcard,
    _is_temporal_query,
    _list_global_repos,
    _mcp_response,
    _parse_json_string_array,
    _truncate_field,
    _truncate_regex_field,
    _validate_symbol_format,
    # Domain helpers in _legacy.py (not yet extracted to domain modules)
    _append_provider_to_config,
    _derive_forge_host,
    _get_cidx_callback_base_url,
    _get_delegation_config,
    _get_delegation_function_repo_path,
    _get_group_manager,
    _get_pat_credential_for_remote,
    _get_personal_credential_for_host,
    _get_repo_ready_timeout,
    _get_user_groups,
    _load_packages_context,
    _lookup_golden_repo_for_cs,
    _omni_search_code,
    _provider_index_job,
    _provider_temporal_index_job,
    _remove_provider_from_config,
    _resolve_cicd_project_access,
    _resolve_cicd_read_token,
    _resolve_cicd_write_token,
    _resolve_git_repo_path,
    _resolve_golden_repo_base_clone,
    _resolve_golden_repo_path,
    _resolve_guardrails,
    _resolve_repo_path,
    _validate_collaborative_params,
    _validate_competitive_params,
    _validate_function_parameters,
    _validate_open_delegation_params,
    _write_mode_acquire_lock,
    _write_mode_create_marker,
    _write_mode_run_refresh,
    _write_mode_strip_global,
    WILDCARD_CHARS,
)
